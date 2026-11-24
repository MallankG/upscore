"""Daily caps on every metered service, sized to stay inside free tiers. Counters live in kv and reset each day.
Override any cap with BUDGET_<NAME>=<n> (0 disables that service)."""
import os
from datetime import date

from . import db

# per day; chosen so a month stays under each free tier
LIMITS = {
    "tavily": 30,            # Tavily free: 1,000 searches/month -> 30/day = 900
    "exa": 30,
    "deepgram_stt": 40,      # Deepgram free credit: voice notes
    "deepgram_tts": 40,      # Pip's spoken replies (each capped at 1,800 chars)
    "pinecone_search": 100,  # Pinecone Starter: well under monthly read units
    "pinecone_upsert": 100,  # batches of <= 90 records
    "meta_llm": 150,         # only used with LLM_PROVIDER=meta (paid); local Ollama is never capped
    "email_in": 60,          # forwarded emails processed per day (also protects the CPU model)
    "email_out": 20,         # AgentMail sends/replies
    "pushover": 50,          # Pushover free: 10,000/month
}


class BudgetExceeded(ValueError):
    """A ValueError so every channel's existing 'tell the user' path handles it."""


def limit(name):
    return int(os.environ.get(f"BUDGET_{name.upper()}", LIMITS[name]))


def used(name, day=None):
    return int(db.kv_get(f"budget:{(day or date.today()).isoformat()}:{name}", 0))


def take(name, n=1, day=None):
    """Spend n units of today's budget or raise BudgetExceeded (nothing is spent on refusal)."""
    day = day or date.today()
    cap, have = limit(name), used(name, day)
    if have + n > cap:
        raise BudgetExceeded(f"Daily free-tier limit reached for {name} ({cap}/day). It resets tomorrow.")
    db.kv_set(f"budget:{day.isoformat()}:{name}", have + n)


def today():
    return {k: {"used": used(k), "limit": limit(k)} for k in LIMITS}
