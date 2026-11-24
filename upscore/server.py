"""Entry point: starts channel pollers + a tiny dashboard/chat web server.  python3 -m upscore.server"""
import base64
import hmac
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import agent, channels, connectors, credit, db, gamify, rewards, voice

PORT = int(os.environ.get("PORT", "8080"))
WEB_TOKEN = os.environ.get("WEB_TOKEN", "")  # set this if the Sprite URL is public: it guards your financial data
PAGE = (Path(__file__).parent / "dashboard.html").read_text()


class Handler(BaseHTTPRequestHandler):
    def authed(self):
        if not WEB_TOKEN:
            return True
        tok = parse_qs(urlparse(self.path).query).get("token", [""])[0] or self.headers.get("X-Token", "")
        return hmac.compare_digest(tok, WEB_TOKEN)

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if not self.authed():
            return self.send(401, "unauthorized: add ?token=", "text/plain")
        if path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/summary":
            accounts, txns = agent.load()
            out = {**credit.summary(accounts, txns, status=db.sub_status()), "profile": agent.profile(),
                   "recent": txns[:15], "inbox": channels.INBOX or None,
                   "scores": db.q("SELECT date, score FROM scores ORDER BY date"),
                   "game": gamify.state(accounts, txns),
                   "cards_where": rewards.cheat_sheet(accounts, db.reward_rules()),
                   "connectors": connectors.status()}
            return self.send(200, json.dumps(out, default=str))
        self.send(404, "not found", "text/plain")

    def do_POST(self):
        if not self.authed():
            return self.send(401, "unauthorized", "text/plain")
        path = urlparse(self.path).path
        if path == "/api/tts":  # Pip's voice through the Deepgram connector
            n = int(self.headers.get("Content-Length", 0))
            text = (json.loads(self.rfile.read(min(n, 20_000)) or b"{}").get("text") or "").strip()
            try:
                out = voice.speak(text) if text else None
            except Exception:
                logging.exception("tts failed")
                out = None
            return self.send(200, out[1], out[0]) if out else self.send(204, b"", "text/plain")
        if path != "/api/chat":
            return self.send(404, "not found", "text/plain")
        n = int(self.headers.get("Content-Length", 0))
        if n > 15_000_000:  # base64 of a 10 MB file
            return self.send(413, "too large", "text/plain")
        body = json.loads(self.rfile.read(n) or b"{}")
        text = (body.get("text") or "").strip()
        try:  # files: [{name, mime, data: base64}] from the dashboard's statement upload
            files = [(f["name"], f["mime"], base64.b64decode(f["data"])) for f in body.get("files", [])]
        except (KeyError, TypeError, ValueError):
            return self.send(400, json.dumps({"error": "bad file payload"}))
        if not text and not files:
            return self.send(400, json.dumps({"error": "empty"}))
        try:
            return self.send(200, json.dumps({"reply": agent.respond("web", text, "web", files)}))
        except ValueError as e:  # unsupported/oversized file, or voice without an STT connector
            return self.send(400, json.dumps({"error": str(e)}))
        except Exception as e:
            logging.exception("chat failed")
            return self.send(502, json.dumps({"error": str(e)}))

    def log_message(self, *a):
        pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.db()
    for fn, every, name in channels.enabled():
        threading.Thread(target=channels.loop, args=(fn, every, name), daemon=True, name=name).start()
        logging.info("started %s", name)
    logging.info("dashboard on :%d", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
