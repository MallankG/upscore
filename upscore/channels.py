"""Inbound/outbound channels: AgentMail (via Sprites connector), Telegram, and the reminder scheduler."""
import logging
import os
import subprocess
import time
import urllib.parse
from datetime import date

from . import agent, credit, db

log = logging.getLogger("upscore")

# AgentMail through the Sprites gateway, e.g. https://api.sprites.dev/v1/gateway/agentmail/<connection_id>
AGENTMAIL_BASE = os.environ.get("AGENTMAIL_BASE_URL", "").rstrip("/")
AGENTMAIL_KEY = os.environ.get("AGENTMAIL_API_KEY", "")  # local dev only; on a Sprite the connector injects it
INBOX = os.environ.get("AGENTMAIL_INBOX", "")              # e.g. upscore@agentmail.to
OWNER_EMAILS = {e.strip().lower() for e in os.environ.get("OWNER_EMAILS", "").split(",") if e.strip()}

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_BASE = os.environ.get("TELEGRAM_API_BASE", f"https://api.telegram.org/bot{TG_TOKEN}").rstrip("/")

EMAIL_POLL_SECS = int(os.environ.get("EMAIL_POLL_SECS", "60"))

# Slack (Bot) connector, e.g. https://api.sprites.dev/v1/gateway/slack/<connection_id>  (direct: https://slack.com/api)
SLACK_BASE = os.environ.get("SLACK_BASE_URL", "").rstrip("/")
SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")    # local dev only; on a Sprite the connector injects it
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")    # channel or DM id the bot watches, e.g. C0123 / D0456
SLACK_POLL_SECS = int(os.environ.get("SLACK_POLL_SECS", "5"))


# ---------- AgentMail ----------

def am(method, path, body=None):
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


def poll_telegram_once():
    offset = int(db.kv_get("telegram_offset", 0))
    for u in tg("getUpdates", {"offset": offset, "timeout": 50}).get("result", []):
        db.kv_set("telegram_offset", u["update_id"] + 1)
        msg = u.get("message") or {}
        text, chat_id = msg.get("text"), (msg.get("chat") or {}).get("id")
        if not text or not chat_id:
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
        tg("sendMessage", {"chat_id": chat_id, "text": agent.respond("telegram", text, "telegram")[:4000]})


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


def poll_slack_once():
    oldest = db.kv_get("slack_oldest")
    if oldest is None:  # first run: start from now, don't replay channel history
        db.kv_set("slack_oldest", f"{time.time():.6f}")
        return
    msgs = slack("conversations.history", params={"channel": SLACK_CHANNEL, "oldest": oldest, "limit": 50}).get("messages", [])
    for m in sorted(msgs, key=lambda m: float(m["ts"])):
        db.kv_set("slack_oldest", m["ts"])
        if m.get("bot_id") or m.get("subtype") or not m.get("text"):
            continue  # skip our own replies, joins, edits
        reply = agent.respond("slack", m["text"], "slack")
        slack("chat.postMessage", {"channel": SLACK_CHANNEL, "text": reply[:3900]})


# ---------- Reminders ----------

def notify(text):
    sent = False
    for fn in (send_telegram, send_slack, lambda t: send_email("Upscore reminder", t)):
        try:
            sent = bool(fn(text)) or sent
        except Exception:
            log.exception("notify failed")
    return sent


def run_reminders_once(today=None):
    accounts = db.q("SELECT * FROM accounts")
    for key, text in credit.reminders(accounts, today or date.today()):
        if not db.kv_get("remind:" + key) and notify(text):
            db.kv_set("remind:" + key, 1)


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
    out.append((run_reminders_once, 3600, "reminders"))
    if os.path.exists(SPRITE_SOCK) and os.environ.get("KEEP_AWAKE", "1") == "1":
        out.append((keepalive_once, 60, "keepalive"))
    return out
