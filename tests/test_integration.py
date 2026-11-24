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
SCRIPT = []      # queued assistant messages; when non-empty they replace fake_llm's default behaviour
TG_QUEUE = []    # extra Telegram updates to deliver


def fake_llm(body):
    """Scripted 'model': records an add_transaction for the user's sentence, then answers in text."""
    if SCRIPT:
        return SCRIPT.pop(0)
    last = body["messages"][-1]
    if last["role"] == "tool":
        return {"role": "assistant", "content": "Logged it."}
    args = {"amount": 84.12, "merchant": "Whole Foods", "kind": "purchase", "account": "Chase Freedom",
            "category": "groceries", "date": "2026-11-22"}
    if "slack" in str(last["content"]):
        args.update(amount=12.5, merchant="Chipotle", category="dining")
    if "telegram" in str(last["content"]):
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
        if self.path.endswith("/photos/p.jpg"):
            b = b"\x89PNG fake"
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        if "conversations.history" in self.path:
            return self.reply({"ok": True, "messages": [
                {"ts": "1795000002.000200", "bot_id": "B1", "text": "earlier bot reply"},
                {"ts": "1795000001.000100", "user": "U1", "text": "slack: lunch 12.50 chipotle"}]})
        self.reply({})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if "/v1/listen" in self.path:  # Deepgram: raw audio in, transcript out
            CALLS.append(("POST", self.path, {"content_type": self.headers["Content-Type"], "bytes": len(raw)}))
            return self.reply({"results": {"channels": [{"alternatives": [{"transcript": "spent twenty dollars on tacos"}]}]}})
        body = json.loads(raw or b"{}")
        CALLS.append(("POST", self.path, body))
        if self.path.endswith("/chat/completions"):
            return self.reply({"choices": [{"message": fake_llm(body)}]})
        if self.path.endswith("/getFile"):
            return self.reply({"ok": True, "result": {"file_path": "photos/p.jpg"}})
        if self.path.endswith("/drafts"):
            return self.reply({"draft_id": "d1"})
        if self.path.endswith("/getUpdates"):
            if body.get("offset"):
                out, TG_QUEUE[:] = list(TG_QUEUE), []
                return self.reply({"ok": True, "result": out})
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
                          TELEGRAM_BOT_TOKEN="x", TELEGRAM_API_BASE=base, DEEPGRAM_BASE_URL=base)
        import importlib
        from upscore import agent, channels, db, voice
        importlib.reload(voice)
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

    def test_01_email_ingest_uses_ember_and_replies_once(self):
        self.channels.poll_email_once()
        self.channels.poll_email_once()  # already seen: no-op
        llm = self.posts("/chat/completions")
        self.assertEqual({b["model"] for b in llm}, {"muse-ember-1.0"})
        self.assertEqual(llm[0]["reasoning_effort"], "low")
        self.assertEqual(len(self.posts("/reply")), 1)
        acct = self.db.q("SELECT balance FROM accounts WHERE name='Chase Freedom'")[0]
        self.assertAlmostEqual(acct["balance"], 84.12)

    def test_02_slack_skips_bots_and_uses_spark(self):
        self.channels.poll_slack_once()  # first run only sets the cursor
        self.assertFalse(self.posts("/chat/completions"))
        self.channels.poll_slack_once()
        self.assertEqual({b["model"] for b in self.posts("/chat/completions")}, {"muse-spark-1.3"})
        sent = self.posts("/chat.postMessage")
        self.assertEqual([s["text"] for s in sent], ["Logged it."])
        self.assertEqual(self.db.kv_get("slack_oldest"), "1795000002.000200")
        self.assertTrue(self.db.q("SELECT 1 FROM txns WHERE merchant='Chipotle' AND source='slack'"))

    def test_03_telegram_claims_owner_and_records_payment(self):
        self.channels.poll_telegram_once()
        self.assertEqual(self.db.kv_get("telegram_chat_id"), "42")
        self.assertEqual(self.posts("/sendMessage")[-1], {"chat_id": 42, "text": "Logged it."})
        bal = self.db.q("SELECT balance FROM accounts WHERE name='Chase Freedom'")[0]["balance"]
        self.assertAlmostEqual(bal, 84.12 + 12.5 - 300)  # email + slack purchases, minus payment

    def test_04_reminders_fan_out_once(self):
        from datetime import date, timedelta
        soon = date.today() + timedelta(days=2)
        self.db.upsert_account("Chase Freedom", credit_limit=1000, balance=500, due_day=soon.day)
        self.channels.run_reminders_once()
        self.channels.run_reminders_once()  # deduped
        self.assertEqual(len(self.posts("/sendMessage")), 1)
        self.assertEqual(len(self.posts("/chat.postMessage")), 1)
        self.assertEqual(len(self.posts("/messages/send")), 1)

    def test_05_web_chat_endpoint(self):
        from upscore import server
        web = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{web.server_port}/api/chat",
                                         data=json.dumps({"text": "web: spent 84.12"}).encode(), method="POST")
            self.assertEqual(json.load(urllib.request.urlopen(req))["reply"], "Logged it.")
        finally:
            web.shutdown()


    def test_06_telegram_photo_goes_to_model_as_image(self):
        TG_QUEUE.append({"update_id": 8, "message": {"chat": {"id": 42}, "caption": "my statement",
                                                     "photo": [{"file_id": "small"}, {"file_id": "big"}]}})
        self.channels.poll_telegram_once()
        self.assertEqual(self.posts("/getFile")[0], {"file_id": "big"})
        content = self.posts("/chat/completions")[0]["messages"][-1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "my statement"})
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertIn("Paper trail", self.posts("/sendMessage")[-1]["text"])  # first upload unlocks it
        hist = self.db.q("SELECT content FROM history WHERE chat='telegram' ORDER BY id DESC LIMIT 2")
        self.assertIn("[attached photo.jpg]", hist[1]["content"])

    def test_07_cancel_draft_then_send_marks_cancelled(self):
        self.db.add_txn(15.49, "Netflix", "purchase", "2026-11-05", "streaming", recurring=True, source="t")
        call = lambda name, args: {"role": "assistant", "content": None, "tool_calls": [  # noqa: E731
            {"id": name, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
        SCRIPT[:] = [call("draft_cancellation", {"merchant": "Netflix", "to": "help@netflix.com",
                                                  "subject": "Cancel my subscription", "body": "Please cancel."}),
                     {"role": "assistant", "content": "Draft ready. Send it?"},
                     call("send_cancellation", {"merchant": "Netflix"}),
                     {"role": "assistant", "content": "Sent."}]
        self.assertEqual(self.agent.respond("web", "cancel netflix", "web"), "Draft ready. Send it?")
        self.assertFalse(self.posts("/send"))  # nothing sent without confirmation
        self.assertEqual(self.agent.respond("web", "yes send it", "web"),
                         "Sent.\n\n🏆 Achievement unlocked: Snip snip (+60 XP)")
        sent = self.posts("/messages/send")
        self.assertEqual(sent[-1], {"to": ["help@netflix.com"], "subject": "Cancel my subscription", "text": "Please cancel."})
        subs = self.agent.call_tool("get_summary", {}, "web")["subscriptions"]
        self.assertNotIn("Netflix", [x["merchant"] for x in subs])

    def test_08_weekly_checkin_once_per_week(self):
        from datetime import datetime
        sunday = datetime(2026, 11, 29, 11)
        self.channels.run_reminders_once(sunday)
        self.channels.run_reminders_once(sunday)
        checkins = [p for p in self.posts("/sendMessage") if "Weekly check-in" in p["text"]]
        self.assertEqual(len(checkins), 1)
        self.channels.run_reminders_once(datetime(2026, 11, 30, 11))  # Monday: nothing new
        self.assertEqual(len([p for p in self.posts("/sendMessage") if "Weekly" in p["text"]]), 1)

    def test_09_simulate_score_and_uncle_persona(self):
        self.agent.call_tool("upsert_account", {"name": "Sim Card", "credit_limit": 1000, "balance": 800}, "web")
        self.agent.call_tool("set_profile", {"key": "credit_score", "value": "650"}, "web")
        sim = self.agent.call_tool("simulate", {"changes": [{"account": "sim card", "amount": 710}]}, "web")
        self.assertAlmostEqual(sim["after"]["per_card"]["Sim Card"], 0.09)
        self.assertGreater(sim["est_score"][0], 650)
        self.assertEqual(self.db.q("SELECT score FROM scores")[-1]["score"], 650)
        self.assertNotIn("Uncle Gary", self.agent.system_prompt())
        self.agent.call_tool("set_profile", {"key": "persona", "value": "uncle"}, "web")
        self.assertIn("Uncle Gary", self.agent.system_prompt())
        self.agent.call_tool("set_profile", {"key": "persona", "value": "normal"}, "web")

    def test_10_dashboard_upload_pdf(self):
        import base64
        from upscore import server
        web = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{web.server_port}/api/chat"
        try:
            pdf = {"name": "stmt.pdf", "mime": "application/pdf", "data": base64.b64encode(b"%PDF-1.4 fake").decode()}
            req = urllib.request.Request(url, data=json.dumps({"files": [pdf]}).encode(), method="POST")
            before = int(self.db.kv_get("count:uploads", 0))
            reply = json.load(urllib.request.urlopen(req))["reply"]
            self.assertTrue(reply.startswith("Logged it."))
            self.assertEqual(int(self.db.kv_get("count:uploads")), before + 1)
            part = self.posts("/chat/completions")[0]["messages"][-1]["content"][1]
            self.assertEqual(part["file"]["filename"], "stmt.pdf")
            bad = {"name": "x.zip", "mime": "application/zip", "data": "AAAA"}
            req = urllib.request.Request(url, data=json.dumps({"files": [bad]}).encode(), method="POST")
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 400)
        finally:
            web.shutdown()


    def test_11_telegram_voice_note_is_transcribed(self):
        TG_QUEUE.append({"update_id": 9, "message": {"chat": {"id": 42}, "voice": {"file_id": "v1", "mime_type": "audio/ogg"}}})
        self.channels.poll_telegram_once()
        listen = [c for c in CALLS if "/v1/listen" in c[1]]
        self.assertEqual(listen[0][2]["content_type"], "audio/ogg")
        self.assertIn("model=nova-3", listen[0][1])
        self.assertEqual(self.posts("/chat/completions")[0]["messages"][-1]["content"], "spent twenty dollars on tacos")
        self.assertTrue(self.posts("/sendMessage")[-1]["text"].startswith("🎙 “spent twenty dollars on tacos”"))

    def test_12_dashboard_audio_and_missing_stt(self):
        import base64
        audio = [("voice-note", "audio/webm;codecs=opus", b"\x1aE fake webm")]
        reply = self.agent.respond("web", "", "web", audio)
        self.assertTrue(reply.startswith("🎙 “spent twenty dollars on tacos”"))
        listen = [c for c in CALLS if "/v1/listen" in c[1]]
        self.assertEqual(listen[-1][2]["content_type"], "audio/webm")  # codec params stripped
        self.agent.voice.DEEPGRAM_BASE = ""
        try:
            with self.assertRaises(ValueError) as e:
                self.agent.respond("web", "", "web", audio)
            self.assertIn("Deepgram", str(e.exception))
        finally:
            self.agent.voice.DEEPGRAM_BASE = os.environ["DEEPGRAM_BASE_URL"]


if __name__ == "__main__":
    unittest.main()
