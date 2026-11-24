"""LLM agent: OpenAI-compatible /chat/completions with tool calling.

LLM_PROVIDER picks a preset (every LLM_* var still overrides it):
  ollama  free: Ollama running on the Sprite itself (default model qwen3.5:4b, tools + vision, CPU-only)
  meta    paid: Meta Model API via the Sprites Meta connector. Muse Spark chats/plans, Muse Ember reads emails.
          Set META_BASE_URL to the connector gateway, e.g. https://api.sprites.dev/v1/gateway/meta/<id>/v1
Outside a Sprite (local dev) set LLM_API_KEY for paid providers.
"""
import base64
import json
import os
import threading
import urllib.error
import urllib.request
from datetime import date

from . import credit, db, gamify, voice

PROVIDERS = {
    "meta": {"base": os.environ.get("META_BASE_URL", "https://api.meta.ai/v1"),
             "model": "muse-spark-1.3", "fast": "muse-ember-1.0", "effort": "low"},  # Muse defaults to high/max: slow
    "ollama": {"base": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
               "model": "qwen3.5:4b", "fast": "qwen3.5:4b", "effort": "none"},     # no thinking: CPU is slow
}
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "meta")
_p = PROVIDERS[LLM_PROVIDER]
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", _p["base"]).rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", _p["model"])
LLM_FAST_MODEL = os.environ.get("LLM_FAST_MODEL", _p["fast"])   # used for email ingestion
LLM_REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", _p["effort"])
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "240"))  # local CPU models can take a while per step
FAST_SOURCES = {"email"}
HISTORY_TURNS = 12
# ponytail: one agent turn at a time, plenty for a single user; per-chat locks if it ever matters
TURN_LOCK = threading.Lock()


def http_json(method, url, body=None, headers=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:  # keep the provider's explanation (e.g. Meta billing, AgentMail permission)
        detail = e.read()[:500].decode(errors="replace")
        raise RuntimeError(f"{e.code} from {url.split('?')[0]}: {detail}") from None


SYSTEM = """You are Upscore, a friendly, concise personal credit coach agent.
You help one user raise their credit score by tracking spending, subscriptions, card balances and due dates,
and turning that into a concrete, dated plan.

Rules:
- Record everything the user tells you with tools: purchases, income, payments, card limits, balances,
  statement and due days. Don't ask for confirmation of obvious things, just record and say what you saved.
- When given an image or PDF (card statement, receipt, banking app screenshot), read it carefully and record
  the card (limit, balance, statement/due day, minimum payment) with upsert_account and every transaction
  with add_transaction. Then say what you captured in one or two lines.
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

UNCLE = """
PERSONA: You are "Uncle Gary", the nosy uncle at Thanksgiving dinner who has OPINIONS about money.
Roast the user's spending with turkey, stuffing and pie metaphors, pass-the-gravy asides, and
"back in my day" energy. Keep it affectionate, never cruel, never about anything but money habits.
Every number still comes from tools, and you still give the real dated plan. One roast line, then the help."""


def system_prompt():
    p = profile()
    extra = UNCLE if p.get("persona") == "uncle" else ""
    return SYSTEM.format(today=date.today().isoformat()) + extra


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
    {"name": "set_profile", "description": "Save a user fact, e.g. key=credit_score value=640 (also logged to score history), "
                                           "key=goal value='700 by June', key=persona value=uncle|normal (Uncle Gary roast mode on/off).",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["key", "value"]}},
    {"name": "get_summary", "description": "Accounts, utilization, 30-day spend by category, income, subscriptions.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_plan", "description": "Prioritized dated credit-improvement actions computed from the data.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "simulate", "description": "What-if: predicted utilization and rough score change if the user pays cards down "
                                        "(and/or adds new charges) before statements close.",
     "parameters": {"type": "object", "properties": {"changes": {"type": "array", "items": {"type": "object", "properties": {
         "account": {"type": "string"},
         "payment": {"type": "number", "description": "Amount the user would pay toward the card, e.g. 1300"},
         "new_charges": {"type": "number", "description": "Amount of new spending on the card, if any"}},
         "required": ["account"]}}},
         "required": ["changes"]}},
    {"name": "draft_cancellation", "description": "Create an email DRAFT cancelling a subscription (not sent). Show the user "
                                                  "the draft and ask them to confirm before sending. Include account email/name if known.",
     "parameters": {"type": "object", "properties": {
         "merchant": {"type": "string"}, "to": {"type": "string", "description": "Merchant support/cancellation email"},
         "subject": {"type": "string"}, "body": {"type": "string"}}, "required": ["merchant", "to", "subject", "body"]}},
    {"name": "send_cancellation", "description": "Send a cancellation draft. ONLY call when the user's latest message explicitly "
                                                 "says to send it.",
     "parameters": {"type": "object", "properties": {"merchant": {"type": "string"}}, "required": ["merchant"]}},
    {"name": "mark_subscription", "description": "Mark a subscription cancelled (status=cancelled) or active again (status=active).",
     "parameters": {"type": "object", "properties": {"merchant": {"type": "string"},
                                                     "status": {"type": "string", "enum": ["cancelled", "active"]}},
                    "required": ["merchant", "status"]}},
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
        if args["key"] == "credit_score":
            db.log_score(args["value"])
        return {"ok": True}
    if name == "simulate":
        accounts, _ = load()
        # signs ignored on purpose: small models often send a payment as a negative number
        net = {c["account"]: abs(c.get("payment") or c.get("amount") or 0) - abs(c.get("new_charges") or 0)
               for c in args["changes"]}
        return credit.simulate(accounts, net, profile().get("credit_score"))
    if name == "draft_cancellation":
        from . import channels
        return channels.create_cancel_draft(args["merchant"], args["to"], args["subject"], args["body"])
    if name == "send_cancellation":
        from . import channels
        return channels.send_cancel_draft(args["merchant"])
    if name == "mark_subscription":
        db.kv_set("sub:" + args["merchant"].strip().lower(), args["status"])
        return {"ok": True}
    if name == "get_summary":
        return {**credit.summary(*load(), status=db.sub_status()), "profile": profile()}
    if name == "get_plan":
        return {"plan": credit.plan(*load(), status=db.sub_status()), "profile": profile()}
    if name == "recent_transactions":
        return db.q("SELECT * FROM txns ORDER BY date DESC, id DESC LIMIT ?", (int(args.get("limit") or 20),))
    return {"error": f"unknown tool {name}"}


def llm(messages, model=None):
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
    body = {"model": model or LLM_MODEL, "messages": messages, "max_tokens": 1500,
            "tools": [{"type": "function", "function": t} for t in TOOLS]}
    if LLM_REASONING_EFFORT:
        body["reasoning_effort"] = LLM_REASONING_EFFORT
    return http_json("POST", LLM_BASE_URL + "/chat/completions", body, headers, LLM_TIMEOUT)["choices"][0]["message"]


MAX_FILE_BYTES = 10_000_000


def user_content(text, files):
    """Chat Completions content parts: images as image_url, PDFs as file parts (both base64 data URLs)."""
    if not files:
        return text
    parts = [{"type": "text", "text": text or "Here's a statement/receipt. Record everything in it."}]
    for name, mime, data in files:
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{name} is over 10 MB")
        url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        if mime.startswith("image/"):
            parts.append({"type": "image_url", "image_url": {"url": url}})
        elif mime == "application/pdf":
            parts.append({"type": "file", "file": {"filename": name, "file_data": url}})
        else:
            raise ValueError(f"unsupported file type {mime}: send an image or PDF")
    return parts


def respond(chat, text, source, files=()):
    """One agent turn. `chat` keys conversation history (e.g. 'telegram', 'email', 'web').
    `files` is a list of (name, mime, bytes): images/PDFs go to the model, audio is transcribed first."""
    heard = [voice.transcribe(m, d) for _, m, d in files if m.startswith("audio/")]  # outside the lock: network
    files = [f for f in files if not f[1].startswith("audio/")]
    if heard:
        text = " ".join([text, *heard]).strip()
    with TURN_LOCK:
        past = db.q("SELECT role, content FROM history WHERE chat=? ORDER BY id DESC LIMIT ?", (chat, HISTORY_TURNS))[::-1]
        messages = [{"role": "system", "content": system_prompt()}, *past,
                    {"role": "user", "content": user_content(text, files)}]
        if files:  # history keeps text only; the file itself is not replayed
            text = (text + " " if text else "") + " ".join(f"[attached {n}]" for n, _, _ in files)
        reply = "Sorry, I got stuck. Try again?"
        for _ in range(8):
            msg = llm(messages, LLM_FAST_MODEL if source in FAST_SOURCES else LLM_MODEL)
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            calls = msg.get("tool_calls") or []
            if not calls:
                # plain text for chat apps: small models add markdown bold even when told not to
                reply = (msg.get("content") or "").replace("**", "").strip() or "Done."
                break
            for c in calls:
                try:
                    out = call_tool(c["function"]["name"], json.loads(c["function"].get("arguments") or "{}"), source)
                except Exception as e:  # surface tool errors to the model so it can recover
                    out = {"error": str(e)}
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(out, default=str)})
        if source != "email":  # forwarded alerts aren't the user checking in
            gamify.record_activity(upload=bool(files))
        fresh = gamify.new_unlocks(*load())
        if fresh:
            reply += "\n\n" + gamify.unlock_text(fresh)
        if heard:  # show what was heard so a mis-transcription is obvious
            reply = f"🎙 “{' '.join(heard)}”\n\n{reply}"
        db.run("INSERT INTO history(chat, role, content) VALUES(?,?,?)", (chat, "user", text[:4000]))
        db.run("INSERT INTO history(chat, role, content) VALUES(?,?,?)", (chat, "assistant", reply))
        return reply
