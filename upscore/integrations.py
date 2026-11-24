"""Optional Sprites connectors. Each one is a no-op (or a clear error for the agent) until connected.

era       bank accounts + transactions, synced into the ledger
pushover  push notifications for reminders and unlocks
tavily    web search (rotating card categories, cancellation pages); exa is the fallback
pinecone  semantic memory over transactions ("how much on coffee since September?")
typesafe  Jev picks a transaction's spending category from a fixed set
box       statement uploads archived to a Box folder
"""
import json
import logging
import os
import urllib.parse
import urllib.request
import uuid
from datetime import date, timedelta

from . import connectors, db, rewards
from .agent import http_json

log = logging.getLogger("upscore")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")      # your Pushover user key (not a secret; the app token is in the connector)
PINECONE_INDEX = os.environ.get("PINECONE_INDEX", "upscore")
BOX_FOLDER = os.environ.get("BOX_FOLDER", "Upscore statements")


def _get(d, *keys, default=None):
    """First present key: Era field names are mapped tolerantly."""
    for k in keys:
        v = d
        for part in k.split("."):
            v = v.get(part) if isinstance(v, dict) else None
        if v not in (None, ""):
            return v
    return default


# ---------- Era: real bank data ----------

def era_sync(days=45):
    """Pull linked accounts and recent transactions into the ledger. Returns counts."""
    base = connectors.base("era")
    if not base:
        return {"error": "Era isn't connected"}
    accts = http_json("GET", base + "/banking/accounts")
    accts = accts.get("accounts", accts) if isinstance(accts, dict) else accts
    names = {}
    for a in accts or []:
        name = _get(a, "displayName", "name", "accountName", "officialName", default="Bank account")
        kind_raw = str(_get(a, "type", "accountType", "subtype", default="")).lower()
        kind = "credit" if "credit" in kind_raw else "savings" if "saving" in kind_raw else "checking"
        bal = _get(a, "currentBalance", "balance.current", "balances.current", "balance")
        lim = _get(a, "creditLimit", "balance.limit", "balances.limit", "limit")
        db.upsert_account(name, kind=kind, balance=abs(float(bal)) if bal is not None else None,
                          credit_limit=float(lim) if lim else None)
        for key in ("id", "accountId", "accountGroupKey"):
            if a.get(key):
                names[str(a[key])] = name
    since = (date.today() - timedelta(days=days)).isoformat()
    added, page = 0, 1
    while page <= 10:
        out = http_json("GET", f"{base}/banking/transactions?page={page}&pageSize=100&fromDate={since}")
        txns = out.get("transactions", [])
        if page == 1 and txns and not db.kv_get("era:sample_logged"):
            log.info("era sample transaction: %s", json.dumps(txns[0])[:800])  # confirms field mapping once
            db.kv_set("era:sample_logged", 1)
        for t in txns:
            tid = str(_get(t, "id", "transactionId", "key", default=""))
            if not tid or db.kv_get("era:" + tid) or _get(t, "pending", "isPending", default=False):
                continue
            amt = float(_get(t, "amount", "amount.value", default=0))
            acct = names.get(str(_get(t, "accountId", "accountGroupKey", "account.id", default="")))
            is_credit = acct and any(a["kind"] == "credit" for a in db.q("SELECT kind FROM accounts WHERE name=?", (acct,)))
            merchant = _get(t, "merchantName", "merchant.name", "merchant", "description", "name", default="Unknown")
            cat = _get(t, "category.name", "categoryName", "category", default=None)
            if amt < 0:
                kind = "purchase"
            elif is_credit:
                kind = "payment" if "payment" in str(merchant).lower() else "refund"
            else:
                kind = "income"
            db.add_txn(abs(amt), str(merchant), kind, str(_get(t, "transactionDate", "date", "postedDate"))[:10],
                       rewards.normalize(cat) if cat else None, acct, False, "era")
            db.kv_set("era:" + tid, 1)
            added += 1
        if page >= int(_get(out, "pagination.totalPages", default=1)):
            break
        page += 1
    for a in accts or []:  # Era's balance is the truth: overwrite the deltas add_txn applied while importing
        name = _get(a, "displayName", "name", "accountName", "officialName", default="Bank account")
        bal = _get(a, "currentBalance", "balance.current", "balances.current", "balance")
        if bal is not None:
            db.upsert_account(name, balance=abs(float(bal)))
    return {"accounts": len(accts or []), "new_transactions": added}


# ---------- Pushover ----------

def push(title, message):
    base = connectors.base("pushover")
    if not (base and PUSHOVER_USER):
        return False
    body = urllib.parse.urlencode({"user": PUSHOVER_USER, "title": title, "message": message[:1000]}).encode()
    req = urllib.request.Request(base + "/1/messages.json", data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r).get("status") == 1


# ---------- web search (Tavily, else Exa) ----------

def web_search(query, n=5):
    if connectors.base("tavily"):
        out = http_json("POST", connectors.base("tavily") + "/search",
                        {"query": query, "max_results": n, "include_answer": True})
        return {"answer": out.get("answer"),
                "results": [{"title": r.get("title"), "url": r.get("url"), "text": (r.get("content") or "")[:600]}
                            for r in out.get("results", [])]}
    if connectors.base("exa"):
        out = http_json("POST", connectors.base("exa") + "/search",
                        {"query": query, "numResults": n, "contents": {"text": {"maxCharacters": 600}}})
        return {"results": [{"title": r.get("title"), "url": r.get("url"), "text": r.get("text")}
                            for r in out.get("results", [])]}
    return {"error": "No web search connector (Tavily or Exa) is connected."}


# ---------- Pinecone: semantic memory over transactions ----------

def _pc(path, body=None, ndjson=None):
    base = connectors.base("pinecone")
    headers = {"X-Pinecone-API-Version": "2025-04"}
    if ndjson is not None:
        req = urllib.request.Request(base + path, data=ndjson.encode(), method="POST",
                                     headers={**headers, "Content-Type": "application/x-ndjson"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    return http_json("POST", base + path, body, headers, timeout=30)


def pinecone_index_new():
    """Upsert transactions not yet indexed (batched from the background loop, never on the chat path)."""
    if not connectors.base("pinecone"):
        return 0
    if not db.kv_get("pinecone:ready"):
        try:  # integrated-embedding index: Pinecone embeds the text itself
            _pc("/indexes/create-for-model", {"name": PINECONE_INDEX, "cloud": "aws", "region": "us-east-1",
                                              "embed": {"model": "llama-text-embed-v2", "field_map": {"text": "text"}}})
        except Exception as e:  # already exists is fine
            log.info("pinecone create-for-model: %s", str(e)[:200])
        db.kv_set("pinecone:ready", 1)
    last = int(db.kv_get("pinecone:last_id", 0))
    rows = db.q("SELECT * FROM txns WHERE id > ? ORDER BY id LIMIT 90", (last,))
    if not rows:
        return 0
    lines = [json.dumps({"_id": f"t{t['id']}", "date": t["date"], "amount": t["amount"], "kind": t["kind"],
                         "text": f"{t['date']} {t['kind']} ${t['amount']:.2f} {t['merchant']} "
                                 f"{t.get('category') or ''} {t.get('account') or ''} {t.get('note') or ''}".strip()})
             for t in rows]
    _pc(f"/indexes/{PINECONE_INDEX}/records/namespaces/txns/upsert", ndjson="\n".join(lines))
    db.kv_set("pinecone:last_id", rows[-1]["id"])
    return len(rows)


def search_history(query, k=12):
    if not connectors.base("pinecone"):
        return {"error": "Pinecone isn't connected; use recent_transactions instead."}
    out = _pc(f"/indexes/{PINECONE_INDEX}/records/namespaces/txns/search",
              {"query": {"inputs": {"text": query}, "top_k": k}, "fields": ["text", "date", "amount", "kind"]})
    hits = out.get("result", {}).get("hits", [])
    return {"matches": [h.get("fields", {}) | {"score": round(h.get("_score", 0), 3)} for h in hits]}


# ---------- TypeSafeAI: Jev picks the category ----------

def categorize(merchant, note=None):
    """One of rewards.CATEGORIES via Jev, or None when not connected / not confident."""
    base = connectors.base("typesafe")
    if not base:
        return None
    out = http_json("POST", base + "/v1/systemone", {
        "model": "jev-latest", "state": f"Card transaction at merchant: {merchant}." + (f" Note: {note}" if note else ""),
        "questions": {"category": {"type": "choice", "instructions": "Which spending category is this purchase",
                                   "criteria": {c: c for c in rewards.CATEGORIES}}}}, timeout=20)
    ans = out.get("answers", {}).get("category", {})
    return ans.get("choice") if ans.get("confidence", 0) >= 0.5 else None


# ---------- Box: archive statements ----------

def box_archive(name, mime, data):
    base = connectors.base("box")
    if not base:
        return None
    folder = db.kv_get("box:folder")
    if not folder:
        try:
            folder = http_json("POST", base + "/2.0/folders", {"name": BOX_FOLDER, "parent": {"id": "0"}})["id"]
        except RuntimeError as e:  # 409: already exists; Box returns its id in the conflict
            body = str(e).split(": ", 1)[-1]
            folder = json.loads(body[body.index("{"):])["context_info"]["conflicts"][0]["id"]
        db.kv_set("box:folder", folder)
    stamp = date.today().isoformat()
    boundary = uuid.uuid4().hex
    attrs = json.dumps({"name": f"{stamp} {name}", "parent": {"id": folder}})
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"attributes\"\r\n\r\n{attrs}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(base + "/2.0/files/content", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["entries"][0]["id"]
