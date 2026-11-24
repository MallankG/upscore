"""Speech-to-text for voice notes (Telegram, dashboard fallback) via the Sprites Deepgram connector.
The dashboard mic normally uses the browser's own free speech recognition and never reaches this."""
import json
import os
import urllib.error
import urllib.request

# e.g. https://api.sprites.dev/v1/gateway/deepgram/<connection_id>  (direct: https://api.deepgram.com)
DEEPGRAM_BASE = os.environ.get("DEEPGRAM_BASE_URL", "").rstrip("/")
DEEPGRAM_KEY = os.environ.get("DEEPGRAM_API_KEY", "")  # local dev only; on a Sprite the connector injects it
DEEPGRAM_MODEL = os.environ.get("DEEPGRAM_MODEL", "nova-3")


def transcribe(mime, data):
    if not DEEPGRAM_BASE:
        raise ValueError("I can't hear voice notes yet: add the Deepgram connector (or use the mic on the dashboard).")
    headers = {"Content-Type": mime.split(";")[0]}
    if DEEPGRAM_KEY:
        headers["Authorization"] = f"Token {DEEPGRAM_KEY}"
    url = f"{DEEPGRAM_BASE}/v1/listen?model={DEEPGRAM_MODEL}&smart_format=true"
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} from Deepgram: {e.read()[:300].decode(errors='replace')}") from None
    text = out["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
    if not text:
        raise ValueError("I couldn't make out any words in that voice note. Try again?")
    return text
