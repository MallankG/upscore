"""End-to-end: every channel against a mock Meta/AgentMail/Slack/Telegram server speaking their wire formats.
Run: python3 -m unittest discover tests"""
import json
import os
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.update(UPSCORE_DB=":memory:", UPSCORE_ENV="/nonexistent")
CALLS = []


def fake_llm(body):
    """Scripted 'model': records an add_transaction for the user's sentence, then answers in text."""
    last = body["messages"][-1]
    if last["role"] == "tool":
        return {"role": "assistant", "content": "Logged it."}
    args = {"amount": 84.12, "merchant": "Whole Foods", "kind": "purchase", "account": "Chase Freedom",
            "category": "groceries", "date": "2026-11-22"}
    if "slack" in last["content"]:
        args.update(amount=12.5, merchant="Chipotle", category="dining")
    if "telegram" in last["content"]:
        args.update(amount=300, merchant="Chase Freedom", kind="payment")
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "add_transaction", "arguments": json.dumps(args)}}]}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        CALLS.append(("GET", self.path, None))
        if "/messages?limit=" in self.path:
            return self.reply({"messages": [
                {"message_id": "<m1@x>", "from": "Me <me@ex.com>", "labels": ["received"]},
                {"message_id": "<m0@x>", "from": "up@agentmail.to", "labels": ["sent"]}]})
        if "/messages/" in self.path:
            return self.reply({"from": "Me <me@ex.com>", "subject": "Fwd: Chase alert", "timestamp": "2026-11-22",
                               "extracted_text": "Your Chase Freedom card was charged $84.12 at Whole Foods."})
        if "conversations.history" in self.path:
            return self.reply({"ok": True, "messages": [
                {"ts": "1795000002.000200", "bot_id": "B1", "text": "earlier bot reply"},
                {"ts": "1795000001.000100", "user": "U1", "text": "slack: lunch 12.50 chipotle"}]})
        self.reply({})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        CALLS.append(("POST", self.path, body))
        if self.path.endswith("/chat/completions"):
            return self.reply({"choices": [{"message": fake_llm(body)}]})
        if self.path.endswith("/getUpdates"):
            if body.get("offset"):
                return self.reply({"ok": True, "result": []})
            return self.reply({"ok": True, "result": [
                {"update_id": 7, "message": {"chat": {"id": 42}, "text": "telegram: paid 300 on chase"}}]})
        self.reply({"ok": True})


class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.srv.server_port}"
        os.environ.update(LLM_BASE_URL=base, AGENTMAIL_BASE_URL=base, AGENTMAIL_INBOX="up@agentmail.to",
                          OWNER_EMAILS="me@ex.com", SLACK_BASE_URL=base, SLACK_CHANNEL="D1",
                          TELEGRAM_BOT_TOKEN="x", TELEGRAM_API_BASE=base)
        import importlib
        from upscore import agent, channels, db
        importlib.reload(agent)
        importlib.reload(channels)
        cls.agent, cls.channels, cls.db = agent, channels, db

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        CALLS.clear()

    def posts(self, suffix):
        return [c[2] for c in CALLS if c[0] == "POST" and c[1].endswith(suffix)]

    def test_1_email_ingest_uses_ember_and_replies_once(self):
        self.channels.poll_email_once()
        self.channels.poll_email_once()  # already seen: no-op
        llm = self.posts("/chat/completions")
        self.assertEqual({b["model"] for b in llm}, {"muse-ember-1.0"})
        self.assertEqual(llm[0]["reasoning_effort"], "low")
        self.assertEqual(len(self.posts("/reply")), 1)
        acct = self.db.q("SELECT balance FROM accounts WHERE name='Chase Freedom'")[0]
        self.assertAlmostEqual(acct["balance"], 84.12)

    def test_2_slack_skips_bots_and_uses_spark(self):
        self.channels.poll_slack_once()  # first run only sets the cursor
        self.assertFalse(self.posts("/chat/completions"))
        self.channels.poll_slack_once()
        self.assertEqual({b["model"] for b in self.posts("/chat/completions")}, {"muse-spark-1.3"})
        sent = self.posts("/chat.postMessage")
        self.assertEqual([s["text"] for s in sent], ["Logged it."])
        self.assertEqual(self.db.kv_get("slack_oldest"), "1795000002.000200")
        self.assertTrue(self.db.q("SELECT 1 FROM txns WHERE merchant='Chipotle' AND source='slack'"))

    def test_3_telegram_claims_owner_and_records_payment(self):
        self.channels.poll_telegram_once()
        self.assertEqual(self.db.kv_get("telegram_chat_id"), "42")
        self.assertEqual(self.posts("/sendMessage")[-1], {"chat_id": 42, "text": "Logged it."})
        bal = self.db.q("SELECT balance FROM accounts WHERE name='Chase Freedom'")[0]["balance"]
        self.assertAlmostEqual(bal, 84.12 + 12.5 - 300)  # email + slack purchases, minus payment

    def test_4_reminders_fan_out_once(self):
        from datetime import date, timedelta
        soon = date.today() + timedelta(days=2)
        self.db.upsert_account("Chase Freedom", credit_limit=1000, balance=500, due_day=soon.day)
        self.channels.run_reminders_once()
        self.channels.run_reminders_once()  # deduped
        self.assertEqual(len(self.posts("/sendMessage")), 1)
        self.assertEqual(len(self.posts("/chat.postMessage")), 1)
        self.assertEqual(len(self.posts("/messages/send")), 1)

    def test_5_web_chat_endpoint(self):
        from upscore import server
        web = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{web.server_port}/api/chat",
                                         data=json.dumps({"text": "web: spent 84.12"}).encode(), method="POST")
            self.assertEqual(json.load(urllib.request.urlopen(req))["reply"], "Logged it.")
        finally:
            web.shutdown()


if __name__ == "__main__":
    unittest.main()
