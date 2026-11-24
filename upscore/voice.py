"""Pip's ears and voice via the Sprites Deepgram connector: speech-to-text for the dashboard mic and Telegram
voice notes, and text-to-speech for spoken replies."""
import json
import os
import urllib.error
import urllib.request

from . import connectors

# e.g. https://api.sprites.dev/v1/gateway/deepgram/<connection_id>  (direct: https://api.deepgram.com)
DEEPGRAM_BASE = connectors.base("deepgram")
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


TTS_MODEL = os.environ.get("DEEPGRAM_TTS_MODEL", "aura-2-thalia-en")


def speak(text):
    """Pip's voice: Deepgram text-to-speech, returns (mime, mp3 bytes) or None when not connected."""
    if not DEEPGRAM_BASE:
        return None
    headers = {"Content-Type": "application/json"}
    if DEEPGRAM_KEY:
        headers["Authorization"] = f"Token {DEEPGRAM_KEY}"
    req = urllib.request.Request(f"{DEEPGRAM_BASE}/v1/speak?model={TTS_MODEL}&encoding=mp3",
                                 data=json.dumps({"text": text[:1800]}).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.headers.get("Content-Type", "audio/mpeg"), r.read()
