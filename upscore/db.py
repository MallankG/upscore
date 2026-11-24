"""SQLite storage. One connection + one lock: this is a single-user personal agent."""
import os
import sqlite3
import threading
from datetime import date

DB_PATH = os.environ.get("UPSCORE_DB", os.path.expanduser("~/.upscore/upscore.db"))
# ponytail: global lock around one connection, fine for one user; per-thread conns if it ever goes multi-user
LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE COLLATE NOCASE NOT NULL,
  kind TEXT NOT NULL DEFAULT 'credit',      -- credit | checking | savings
  credit_limit REAL,
  balance REAL NOT NULL DEFAULT 0,
  statement_day INTEGER,                     -- day of month statement closes
  due_day INTEGER,                           -- day of month payment is due
  min_payment REAL,
  autopay INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS txns (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  amount REAL NOT NULL,                      -- always positive; kind gives direction
  merchant TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'purchase',     -- purchase | payment | income | refund
  category TEXT,
  account TEXT,
  recurring INTEGER NOT NULL DEFAULT 0,
  source TEXT,                               -- email | telegram | web
  note TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, chat TEXT, role TEXT, content TEXT);
CREATE TABLE IF NOT EXISTS card_rewards (       -- % back by category; start/end set for rotating offers
  id INTEGER PRIMARY KEY, card TEXT NOT NULL COLLATE NOCASE, category TEXT NOT NULL, rate REAL NOT NULL,
  start TEXT, end TEXT, cap REAL, note TEXT,
  UNIQUE(card, category, start));
CREATE TABLE IF NOT EXISTS scores (date TEXT PRIMARY KEY, score INTEGER NOT NULL);  -- one per day, last wins
"""


def connect(path=DB_PATH):
    if path != ":memory:":
        os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


CONN = None


def db():
    global CONN
    if CONN is None:
        CONN = connect()
    return CONN


def q(sql, args=()):
    with LOCK:
        return [dict(r) for r in db().execute(sql, args).fetchall()]


def run(sql, args=()):
    with LOCK:
        cur = db().execute(sql, args)
        db().commit()
        return cur.lastrowid


def kv_get(key, default=None):
    rows = q("SELECT value FROM kv WHERE key=?", (key,))
    return rows[0]["value"] if rows else default


def kv_set(key, value):
    run("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def log_score(score, date_=None):
    score = int(float(score))
    if not 300 <= score <= 850:
        raise ValueError("credit scores run 300-850")
    run("INSERT INTO scores(date, score) VALUES(?,?) ON CONFLICT(date) DO UPDATE SET score=excluded.score",
        (date_ or date.today().isoformat(), score))


def set_reward(card, category, rate, start=None, end=None, cap=None, note=None):
    run("""INSERT INTO card_rewards(card, category, rate, start, end, cap, note) VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(card, category, start) DO UPDATE SET rate=excluded.rate, end=excluded.end,
           cap=excluded.cap, note=excluded.note""", (card, category, float(rate), start, end, cap, note))


def reward_rules():
    return q("SELECT * FROM card_rewards")


def sub_status():
    """merchant(lowercase) -> 'cancelled' | 'active' as set by the user."""
    return {r["key"][4:]: r["value"] for r in q("SELECT key, value FROM kv WHERE key LIKE 'sub:%'")}


# --- domain writes (used by agent tools) ---

def upsert_account(name, kind=None, credit_limit=None, balance=None, statement_day=None,
                   due_day=None, min_payment=None, autopay=None):
    with LOCK:
        if not q("SELECT 1 FROM accounts WHERE name=?", (name,)):
            run("INSERT INTO accounts(name, kind) VALUES(?, ?)", (name, kind or "credit"))
        fields = dict(kind=kind, credit_limit=credit_limit, balance=balance, statement_day=statement_day,
                      due_day=due_day, min_payment=min_payment,
                      autopay=None if autopay is None else int(bool(autopay)))
        for col, val in fields.items():
            if val is not None:
                run(f"UPDATE accounts SET {col}=? WHERE name=?", (val, name))
        return q("SELECT * FROM accounts WHERE name=?", (name,))[0]


def add_txn(amount, merchant, kind="purchase", date_=None, category=None, account=None,
            recurring=False, source=None, note=None):
    """Insert a transaction and move the linked credit balance. Dedupes identical rows (same email twice)."""
    amount = abs(float(amount))
    date_ = date_ or date.today().isoformat()
    with LOCK:
        dup = q("SELECT id FROM txns WHERE date=? AND amount=? AND merchant=? AND kind=? AND IFNULL(account,'')=IFNULL(?,'')",
                (date_, amount, merchant, kind, account))
        if dup:
            return {"id": dup[0]["id"], "duplicate": True}
        if account:
            acct = q("SELECT * FROM accounts WHERE name=?", (account,))
            if not acct:
                upsert_account(account)
                acct = q("SELECT * FROM accounts WHERE name=?", (account,))
            if acct[0]["kind"] == "credit":
                delta = amount if kind == "purchase" else -amount if kind in ("payment", "refund") else 0
                run("UPDATE accounts SET balance=balance+? WHERE name=?", (delta, account))
        tid = run("INSERT INTO txns(date,amount,merchant,kind,category,account,recurring,source,note) VALUES(?,?,?,?,?,?,?,?,?)",
                  (date_, amount, merchant, kind, category, account, int(bool(recurring)), source, note))
        return {"id": tid, "duplicate": False}
