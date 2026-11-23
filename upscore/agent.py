"""LLM agent: OpenAI-compatible /chat/completions with tool calling.

Default: Meta Model API via the Sprites Meta connector. Muse Spark plans and chats; Muse Ember (cheaper,
built for high-volume tool calls) handles email extraction. Inside a Sprite LLM_BASE_URL is the gateway, e.g.
  https://api.sprites.dev/v1/gateway/meta/<connection_id>/v1
and no key is needed. Outside a Sprite (local dev), set LLM_API_KEY. Any OpenAI-compatible endpoint works.
"""
import json
import os
import threading
import urllib.request
from datetime import date

from . import credit, db

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.meta.ai/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "muse-spark-1.3")
LLM_FAST_MODEL = os.environ.get("LLM_FAST_MODEL", "muse-ember-1.0")   # used for email ingestion
LLM_REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "low")  # Muse defaults to high/max: slow for chat
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
FAST_SOURCES = {"email"}
HISTORY_TURNS = 12
# ponytail: one agent turn at a time, plenty for a single user; per-chat locks if it ever matters
TURN_LOCK = threading.Lock()


def http_json(method, url, body=None, headers=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


SYSTEM = """You are Upscore, a friendly, concise personal credit coach agent.
You help one user raise their credit score by tracking spending, subscriptions, card balances and due dates,
and turning that into a concrete, dated plan.

Rules:
- Record everything the user tells you with tools: purchases, income, payments, card limits, balances,
  statement and due days. Don't ask for confirmation of obvious things, just record and say what you saved.
- When given an email (bank alert, receipt, statement, subscription notice), extract every transaction or
  account fact it contains and record it. Card payment confirmations are kind=payment. Paychecks/deposits are
  kind=income. Mark subscriptions/renewals recurring=true. Use the card/account name if mentioned (e.g. "Chase Freedom").
  If an email has nothing financial, do nothing.
- NEVER invent numbers. For plans, utilization, dues or subscriptions call get_plan/get_summary and quote them.
- Plans: lead with the 1-3 highest-impact actions, each with an amount and a date. Explain the "why" in one line.
- Warn against closing old cards, opening many new cards, or paying only after the statement date when utilization is high.
- You give educational guidance based on public credit-scoring factors, not licensed financial advice. Don't pick investments.
- Keep replies short and chat-friendly (this is read on a phone). Plain text, no markdown tables.
Today is {today}."""

TOOLS = [
    {"name": "add_transaction", "description": "Record a purchase, payment to a card, income, or refund.",
     "parameters": {"type": "object", "properties": {
         "amount": {"type": "number", "description": "Positive amount"},
         "merchant": {"type": "string", "description": "Merchant, payer, or card being paid"},
         "kind": {"type": "string", "enum": ["purchase", "payment", "income", "refund"]},
         "date": {"type": "string", "description": "YYYY-MM-DD, default today"},
         "category": {"type": "string", "description": "e.g. groceries, dining, streaming, rent, gas, salary"},
         "account": {"type": "string", "description": "Card/account name the money moved on"},
         "recurring": {"type": "boolean", "description": "true for subscriptions/memberships/bills"},
         "note": {"type": "string"}},
         "required": ["amount", "merchant", "kind"]}},
    {"name": "upsert_account", "description": "Create or update a credit card or bank account. Only pass fields you know.",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string"}, "kind": {"type": "string", "enum": ["credit", "checking", "savings"]},
         "credit_limit": {"type": "number"}, "balance": {"type": "number", "description": "Current balance owed (credit) or held"},
         "statement_day": {"type": "integer", "description": "Day of month the statement closes (1-31)"},
         "due_day": {"type": "integer", "description": "Day of month payment is due (1-31)"},
         "min_payment": {"type": "number"}, "autopay": {"type": "boolean"}},
         "required": ["name"]}},
    {"name": "set_profile", "description": "Save a user fact, e.g. key=credit_score value=640, key=goal value='700 by June'.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["key", "value"]}},
    {"name": "get_summary", "description": "Accounts, utilization, 30-day spend by category, income, subscriptions.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_plan", "description": "Prioritized dated credit-improvement actions computed from the data.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "recent_transactions", "description": "List recent transactions.",
     "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
]


def load():
    return db.q("SELECT * FROM accounts"), db.q("SELECT * FROM txns ORDER BY date DESC")


def profile():
    return {r["key"][8:]: r["value"] for r in db.q("SELECT * FROM kv WHERE key LIKE 'profile:%'")}


def call_tool(name, args, source):
    if name == "add_transaction":
        return db.add_txn(args["amount"], args["merchant"], args.get("kind", "purchase"), args.get("date"),
                          args.get("category"), args.get("account"), args.get("recurring", False), source, args.get("note"))
    if name == "upsert_account":
        return db.upsert_account(**{k: v for k, v in args.items() if k in db.upsert_account.__code__.co_varnames})
    if name == "set_profile":
        db.kv_set("profile:" + args["key"], args["value"])
        return {"ok": True}
    if name == "get_summary":
        return {**credit.summary(*load()), "profile": profile()}
    if name == "get_plan":
        return {"plan": credit.plan(*load()), "profile": profile()}
    if name == "recent_transactions":
        return db.q("SELECT * FROM txns ORDER BY date DESC, id DESC LIMIT ?", (int(args.get("limit") or 20),))
    return {"error": f"unknown tool {name}"}


def llm(messages, model=None):
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
    body = {"model": model or LLM_MODEL, "messages": messages, "max_tokens": 1500,
            "tools": [{"type": "function", "function": t} for t in TOOLS]}
    if LLM_REASONING_EFFORT:
        body["reasoning_effort"] = LLM_REASONING_EFFORT
    return http_json("POST", LLM_BASE_URL + "/chat/completions", body, headers)["choices"][0]["message"]


def respond(chat, text, source):
    """One agent turn. `chat` keys conversation history (e.g. 'telegram', 'email', 'web')."""
    with TURN_LOCK:
        past = db.q("SELECT role, content FROM history WHERE chat=? ORDER BY id DESC LIMIT ?", (chat, HISTORY_TURNS))[::-1]
        messages = [{"role": "system", "content": SYSTEM.format(today=date.today().isoformat())}, *past,
                    {"role": "user", "content": text}]
        reply = "Sorry, I got stuck. Try again?"
        for _ in range(8):
            msg = llm(messages, LLM_FAST_MODEL if source in FAST_SOURCES else LLM_MODEL)
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = (msg.get("content") or "").strip() or "Done."
                break
            for c in calls:
                try:
                    out = call_tool(c["function"]["name"], json.loads(c["function"].get("arguments") or "{}"), source)
                except Exception as e:  # surface tool errors to the model so it can recover
                    out = {"error": str(e)}
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(out, default=str)})
        db.run("INSERT INTO history(chat, role, content) VALUES(?,?,?)", (chat, "user", text[:4000]))
        db.run("INSERT INTO history(chat, role, content) VALUES(?,?,?)", (chat, "assistant", reply))
        return reply
