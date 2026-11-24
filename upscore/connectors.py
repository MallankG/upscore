"""Find connector gateway URLs. On a Sprite, ask the gateway which connectors this Sprite may use, so a
connector added in the Fly dashboard works after a restart with no config. `<PROVIDER>_BASE_URL` overrides."""
import json
import logging
import os
import urllib.request

GATEWAY_LIST = "https://api.sprites.dev/v1/gateway/list"
ENV_NAMES = {"slack_bot": "SLACK_BASE_URL"}  # legacy names
_found = None


def discover():
    global _found
    if _found is None:
        _found = {}
        if os.path.exists("/.sprite/api.sock") and os.environ.get("CONNECTOR_DISCOVERY", "1") == "1":
            try:
                with urllib.request.urlopen(GATEWAY_LIST, timeout=10) as r:
                    for c in json.load(r).get("connections", []):
                        _found.setdefault(c["provider"], c["gateway_base_url"])
            except Exception:
                logging.exception("connector discovery failed; using env vars only")
    return _found


def base(provider):
    """Gateway base URL for a provider ('' when not connected)."""
    env = os.environ.get(ENV_NAMES.get(provider, provider.upper() + "_BASE_URL"))
    return (env if env is not None else discover().get(provider, "")).rstrip("/")


def status():
    names = ["meta", "agentmail", "slack_bot", "deepgram", "era", "pushover", "tavily", "exa", "pinecone", "typesafe", "box"]
    return {n: bool(base(n)) for n in names}
