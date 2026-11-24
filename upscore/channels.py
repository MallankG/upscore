"""Inbound/outbound channels: AgentMail (via Sprites connector), Telegram, and the reminder scheduler."""
import json
import logging
import re
import os
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import date

from . import agent, budget, connectors, credit, db, gamify, integrations

log = logging.getLogger("upscore")

# AgentMail through the Sprites gateway, e.g. https://api.sprites.dev/v1/gateway/agentmail/<connection_id>
AGENTMAIL_BASE = connectors.base("agentmail")
AGENTMAIL_KEY = os.environ.get("AGENTMAIL_API_KEY", "")  # local dev only; on a Sprite the connector injects it
INBOX = os.environ.get("AGENTMAIL_INBOX", "")              # e.g. upscore@agentmail.to
OWNER_EMAILS = {e.strip().lower() for e in os.environ.get("OWNER_EMAILS", "").split(",") if e.strip()}

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_BASE = os.environ.get("TELEGRAM_API_BASE", f"https://api.telegram.org/bot{TG_TOKEN}").rstrip("/")

EMAIL_POLL_SECS = int(os.environ.get("EMAIL_POLL_SECS", "60"))

# Slack (Bot) connector, e.g. https://api.sprites.dev/v1/gateway/slack/<connection_id>  (direct: https://slack.com/api)
SLACK_BASE = connectors.base("slack_bot")
SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")    # local dev only; on a Sprite the connector injects it
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")    # channel or DM id the bot watches, e.g. C0123 / D0456
SLACK_POLL_SECS = int(os.environ.get("SLACK_POLL_SECS", "65"))  # Slack allows ~1 history call/min for unlisted apps


# ---------- AgentMail ----------

def am(method, path, body=None):
    if method == "POST" and (path.endswith("/send") or path.endswith("/reply")):
        budget.take("email_out")  # every outgoing email (replies, reminders, cancellations) passes here
    headers = {"Authorization": f"Bearer {AGENTMAIL_KEY}"} if AGENTMAIL_KEY else {}
    return agent.http_json(method, AGENTMAIL_BASE + path, body, headers, timeout=30)


def inbox_path():
    return "/v0/inboxes/" + urllib.parse.quote(INBOX, safe="")


def sender_addr(frm):
    frm = frm or ""
    return (frm.split("<")[-1].rstrip(">") if "<" in frm else frm).strip().lower()


def poll_email_once():
    msgs = am("GET", inbox_path() + "/messages?limit=25").get("messages", [])
    for m in reversed(msgs):  # oldest first
        mid = m["message_id"]
        if db.kv_get("seen:" + mid) or "sent" in (m.get("labels") or []):
            continue
        try:
            budget.take("email_in")
        except budget.BudgetExceeded:
            log.warning("email_in budget reached; leaving the rest unread until tomorrow")
            return
        db.kv_set("seen:" + mid, 1)
        full = am("GET", f"{inbox_path()}/messages/{urllib.parse.quote(mid, safe='')}")
        body = full.get("extracted_text") or full.get("text") or full.get("preview") or ""
        frm = full.get("from") or m.get("from")
        prompt = (f"[Email received]\nFrom: {frm}\nSubject: {full.get('subject')}\nDate: {full.get('timestamp')}\n\n"
                  f"{body[:12000]}")
        reply = agent.respond("email", prompt, "email")
        log.info("email %s processed", mid)
        # Only reply to the owner: never auto-reply to banks or no-reply senders.
        if sender_addr(frm) in OWNER_EMAILS:
            am("POST", f"{inbox_path()}/messages/{urllib.parse.quote(mid, safe='')}/reply", {"text": reply})


def send_email(subject, text):
    if AGENTMAIL_BASE and INBOX and OWNER_EMAILS:
        am("POST", inbox_path() + "/messages/send", {"to": sorted(OWNER_EMAILS), "subject": subject, "text": text})
        return True
    return False


# ---------- Telegram ----------

def tg(method, body=None):
    return agent.http_json("POST", f"{TG_BASE}/{method}", body or {}, timeout=60)


def send_telegram(text):
    chat = db.kv_get("telegram_chat_id")
    if TG_TOKEN and chat:
        tg("sendMessage", {"chat_id": int(chat), "text": text[:4000]})
        return True
    return False


def telegram_file(msg):
    """(name, mime, bytes) for a photo (largest size) or document."""
    if msg.get("photo"):
        fid, name, mime = msg["photo"][-1]["file_id"], "photo.jpg", "image/jpeg"
    elif msg.get("voice") or msg.get("audio"):
        v = msg.get("voice") or msg["audio"]
        fid, name, mime = v["file_id"], "voice.ogg", v.get("mime_type", "audio/ogg")
    else:
        d = msg["document"]
        fid, name, mime = d["file_id"], d.get("file_name", "file"), d.get("mime_type", "application/octet-stream")
    path = tg("getFile", {"file_id": fid})["result"]["file_path"]
    file_base = TG_BASE.replace("/bot", "/file/bot", 1)
    with urllib.request.urlopen(f"{file_base}/{path}", timeout=60) as r:
        return name, mime, r.read(agent.MAX_FILE_BYTES + 1)


def poll_telegram_once():
    offset = int(db.kv_get("telegram_offset", 0))
    for u in tg("getUpdates", {"offset": offset, "timeout": 50}).get("result", []):
        db.kv_set("telegram_offset", u["update_id"] + 1)
        msg = u.get("message") or {}
        text, chat_id = msg.get("text") or msg.get("caption") or "", (msg.get("chat") or {}).get("id")
        has_file = msg.get("photo") or msg.get("document") or msg.get("voice") or msg.get("audio")
        if not (text or has_file) or not chat_id:
            continue
        owner = db.kv_get("telegram_chat_id") or os.environ.get("TELEGRAM_CHAT_ID")
        if not owner:  # first person to /start the bot owns it: this is a personal agent
            db.kv_set("telegram_chat_id", chat_id)
            owner = str(chat_id)
        if str(chat_id) != str(owner):
            tg("sendMessage", {"chat_id": chat_id, "text": "This Upscore agent is private."})
            continue
        db.kv_set("telegram_chat_id", chat_id)
        if text.strip() == "/start":
            text = "Hi! Introduce yourself briefly and tell me what you can do and what info you need from me."
        tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        try:
            files = [telegram_file(msg)] if has_file else []
            reply = agent.respond("telegram", text, "telegram", files)
        except ValueError as e:  # unsupported/oversized file
            reply = str(e)
        tg("sendMessage", {"chat_id": chat_id, "text": reply[:4000]})


# ---------- Slack ----------
# Polls one channel/DM with conversations.history: no public webhook needed, works from a sleeping Sprite.

def slack(method, body=None, params=None):
    headers = {"Authorization": f"Bearer {SLACK_TOKEN}"} if SLACK_TOKEN else {}
    url = f"{SLACK_BASE}/{method}" + ("?" + urllib.parse.urlencode(params) if params else "")
    out = agent.http_json("POST" if body is not None else "GET", url, body, headers, timeout=30)
    if not out.get("ok"):
        raise RuntimeError(f"slack {method}: {out.get('error')}")
    return out


def send_slack(text):
    if SLACK_BASE and SLACK_CHANNEL:
        slack("chat.postMessage", {"channel": SLACK_CHANNEL, "text": text[:3900]})
        return True
    return False


def slack_file(f):
    # ponytail: Slack serves files from files.slack.com, which the connector gateway doesn't proxy;
    # downloads need SLACK_BOT_TOKEN (files:read). Upload via Telegram or the dashboard otherwise.
    if not SLACK_TOKEN:
        raise PermissionError("I can't open Slack files through the connector yet. Upload it on the dashboard or send it on Telegram.")
    req = urllib.request.Request(f["url_private_download"], headers={"Authorization": f"Bearer {SLACK_TOKEN}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return f.get("name", "file"), f.get("mimetype", "application/octet-stream"), r.read(agent.MAX_FILE_BYTES + 1)


def poll_slack_once():
    oldest = db.kv_get("slack_oldest")
    if oldest is None:  # first run: start from now, don't replay channel history
        db.kv_set("slack_oldest", f"{time.time():.6f}")
        return
    msgs = slack("conversations.history", params={"channel": SLACK_CHANNEL, "oldest": oldest, "limit": 50}).get("messages", [])
    for m in sorted(msgs, key=lambda m: float(m["ts"])):
        db.kv_set("slack_oldest", m["ts"])
        if m.get("bot_id") or (m.get("subtype") not in (None, "file_share")) or not (m.get("text") or m.get("files")):
            continue  # skip our own replies, joins, edits
        text = re.sub(r"<@[A-Z0-9]+>", "", m.get("text", "")).strip()  # drop Slack @mention tags
        try:
            reply = agent.respond("slack", text, "slack", [slack_file(f) for f in m.get("files", [])])
        except (ValueError, PermissionError) as e:
            reply = str(e)
        slack("chat.postMessage", {"channel": SLACK_CHANNEL, "text": reply[:3900]})


# ---------- Reminders ----------

def notify(text):
    sent = False
    for fn in (send_telegram, send_slack, lambda t: integrations.push("Upscore", t),
               lambda t: send_email("Upscore reminder", t)):
        try:
            sent = bool(fn(text)) or sent
        except Exception:
            log.exception("notify failed")
    return sent


CHECKIN_WEEKDAY = int(os.environ.get("CHECKIN_WEEKDAY", "6"))  # 0=Mon .. 6=Sun
CHECKIN_HOUR = int(os.environ.get("CHECKIN_HOUR", "10"))


def run_reminders_once(now=None):
    from datetime import datetime
    now = now or datetime.now()
    accounts, txns = agent.load()
    for key, text in credit.reminders(accounts, now.date(), txns=txns):
        if not db.kv_get("remind:" + key) and notify(text):
            db.kv_set("remind:" + key, 1)
    with agent.TURN_LOCK:  # don't race an agent turn for the same unlocks
        fresh = gamify.new_unlocks(accounts, txns, now.date())
    if fresh:
        notify(gamify.unlock_text(fresh))
    week = "checkin:%d-W%02d" % now.isocalendar()[:2]
    if now.weekday() == CHECKIN_WEEKDAY and now.hour >= CHECKIN_HOUR and not db.kv_get(week) and accounts:
        if notify(credit.checkin(accounts, txns, now.date(), db.sub_status())):
            db.kv_set(week, 1)


# ---------- Subscription cancellation (AgentMail drafts: nothing is sent without the user's OK) ----------

def create_cancel_draft(merchant, to, subject, body):
    """Stores the pending email locally (works with inbox-scoped AgentMail keys that lack draft permissions)."""
    draft = {"to": to, "subject": subject, "body": body}
    db.kv_set("draft:" + merchant.strip().lower(), json.dumps(draft))
    if not (AGENTMAIL_BASE and INBOX):
        return {**draft, "note": "AgentMail isn't configured, so the user must send this themselves."}
    return {**draft, "next": "Show this to the user and ask them to confirm before sending."}


def send_cancel_draft(merchant):
    key = merchant.strip().lower()
    raw = db.kv_get("draft:" + key)
    if not raw:
        return {"error": f"no draft for {merchant}; create one first"}
    if not (AGENTMAIL_BASE and INBOX):
        return {"error": "AgentMail isn't configured; the user has to send it themselves"}
    d = json.loads(raw)
    am("POST", inbox_path() + "/messages/send", {"to": [d["to"]], "subject": d["subject"], "text": d["body"]})
    db.run("DELETE FROM kv WHERE key=?", ("draft:" + key,))
    db.kv_set("sub:" + key, "cancelled")
    return {"sent": True, "marked": "cancelled"}


# ---------- Sprite keep-alive ----------
# Pollers need the Sprite awake. A 5-minute Task refreshed every minute expires by itself if we crash.
SPRITE_SOCK = "/.sprite/api.sock"


def keepalive_once():
    api = ["curl", "-sf", "--unix-socket", SPRITE_SOCK, "-H", "Content-Type: application/json"]
    if subprocess.run(api + ["-X", "PUT", "http://sprite/v1/tasks/upscore", "-d", '{"expire":"5m"}'],
                      capture_output=True).returncode:
        subprocess.run(api + ["-X", "POST", "http://sprite/v1/tasks", "-d", '{"name":"upscore","expire":"5m"}'],
                       capture_output=True, check=True)


# ---------- loops ----------

def loop(fn, every, name):
    while True:
        try:
            fn()
        except agent.RateLimited as e:  # back off exactly as long as the provider asked
            log.warning("%s rate-limited; waiting %.0fs", name, e.wait)
            time.sleep(e.wait)
        except Exception:
            log.exception("%s failed", name)
            time.sleep(10)
        time.sleep(every)


def enabled():
    out = []
    if AGENTMAIL_BASE and INBOX:
        out.append((poll_email_once, EMAIL_POLL_SECS, "email"))
    if TG_TOKEN:
        out.append((poll_telegram_once, 0, "telegram"))  # long-poll blocks itself
    if SLACK_BASE and SLACK_CHANNEL:
        out.append((poll_slack_once, SLACK_POLL_SECS, "slack"))
    out.append((run_reminders_once, 900, "reminders"))
    if connectors.base("era"):
        out.append((integrations.era_sync, 1800, "era"))
    if connectors.base("pinecone"):
        out.append((integrations.pinecone_index_new, 120, "pinecone"))
    if os.path.exists(SPRITE_SOCK) and os.environ.get("KEEP_AWAKE", "1") == "1":
        out.append((keepalive_once, 60, "keepalive"))
    return out
