"""Gamification: XP, levels, Pip's growth stage + mood, daily streak, achievements and the milestone path.
Everything is recomputed from data (txns, scores, kv), so it can never drift out of sync."""
from datetime import date, timedelta

from . import credit, db

# id, title, description, icon, xp, metric, target
ACHIEVEMENTS = [
    ("card_added", "Card on file", "Tell Pip about a credit card", "card", 20, "cards", 1),
    ("first_log", "First receipt", "Log your first transaction", "receipt", 20, "txns", 1),
    ("scanner", "Paper trail", "Send a statement photo or PDF", "scan", 40, "uploads", 1),
    ("logger_25", "Bookkeeper", "Log 25 transactions", "receipt", 50, "txns", 25),
    ("logger_100", "Ledger legend", "Log 100 transactions", "receipt", 150, "txns", 100),
    ("ontime_1", "On time", "A full cycle paid on time on every card", "shield", 50, "ontime", 1),
    ("ontime_3", "Clean quarter", "3 months on time", "shield", 120, "ontime", 3),
    ("ontime_6", "Half-year hero", "6 months on time", "shield", 250, "ontime", 6),
    ("ontime_12", "Perfect year", "12 months on time", "shield", 500, "ontime", 12),
    ("util_30", "Under 30", "Overall utilization at or below 30%", "gauge", 80, "util30", 1),
    ("util_10", "Single digits", "Overall utilization under 10%", "gauge", 150, "util10", 1),
    ("daily_7", "Week warrior", "Check in 7 days in a row", "flame", 70, "daily", 7),
    ("daily_30", "Habit locked", "Check in 30 days in a row", "flame", 200, "daily", 30),
    ("score_10", "Up ten", "Score up 10 points", "trend", 60, "gain", 10),
    ("score_25", "Up twenty-five", "Score up 25 points", "trend", 120, "gain", 25),
    ("score_50", "Up fifty", "Score up 50 points", "trend", 250, "gain", 50),
    ("goal", "Goal crusher", "Reach your goal score", "flag", 500, "goal", 1),
    ("snip", "Snip snip", "Cancel a subscription", "scissors", 60, "cancels", 1),
    ("uncle", "Survived Uncle Gary", "Turn on Uncle Gary mode", "turkey", 30, "uncle", 1),
]
PATH = ["card_added", "first_log", "scanner", "ontime_1", "util_30", "score_10",
        "ontime_3", "util_10", "score_25", "ontime_6", "goal", "ontime_12"]
STAGES = [(1, "Seed"), (2, "Sprout"), (4, "Sapling"), (7, "Bloom"), (10, "Mighty oak")]


def record_activity(today=None, upload=False):
    """Called on every agent turn: marks today active (daily streak) and counts statement uploads."""
    db.kv_set("day:" + (today or date.today()).isoformat(), 1)
    if upload:
        db.kv_set("count:uploads", int(db.kv_get("count:uploads", 0)) + 1)


def daily_streak(today=None):
    today = today or date.today()
    days = {r["key"][4:] for r in db.q("SELECT key FROM kv WHERE key LIKE 'day:%'")}
    d = today if today.isoformat() in days else today - timedelta(days=1)  # today not done yet keeps the streak
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n, today.isoformat() in days


def level_for(xp):
    """Level n -> n+1 costs 100 + 50(n-1) XP. Returns (level, xp into level, xp needed for next)."""
    level, need = 1, 100
    while xp >= need:
        xp -= need
        level += 1
        need = 100 + 50 * (level - 1)
    return level, xp, need


def metrics(accounts, txns, today):
    scores = db.q("SELECT score FROM scores ORDER BY date")
    goal_txt = db.kv_get("profile:goal", "") or ""
    goal = next((int(w) for w in goal_txt.replace(",", " ").split() if w.isdigit() and 300 <= int(w) <= 850), None)
    st = credit.streaks(accounts, txns, today)
    u = credit.utilization(accounts)["overall"]
    return {
        "cards": sum(a["kind"] == "credit" for a in accounts),
        "txns": len(txns),
        "uploads": int(db.kv_get("count:uploads", 0)),
        "ontime": min(st.values()) if st else 0,
        "util30": int(u is not None and u <= 0.30),
        "util10": int(u is not None and u < 0.10),
        "daily": daily_streak(today)[0],
        "gain": (scores[-1]["score"] - scores[0]["score"]) if len(scores) > 1 else 0,
        "goal": int(bool(goal and scores and scores[-1]["score"] >= goal)),
        "cancels": sum(v == "cancelled" for v in db.sub_status().values()),
        "uncle": int(db.kv_get("profile:persona") == "uncle" or bool(db.kv_get("ach:uncle"))),
        "_util": u, "_streaks": st, "_scores": scores,
    }


def state(accounts, txns, today=None):
    today = today or date.today()
    m = metrics(accounts, txns, today)
    achs = []
    for aid, title, desc, icon, xp, metric, target in ACHIEVEMENTS:
        cur = m[metric]
        achs.append({"id": aid, "title": title, "desc": desc, "icon": icon, "xp": xp,
                     "progress": min(cur, target), "target": target, "unlocked": cur >= target,
                     "unlocked_at": db.kv_get("ach:" + aid)})
    payments = sum(t["kind"] == "payment" for t in txns)
    active_days = len(db.q("SELECT 1 FROM kv WHERE key LIKE 'day:%'"))
    xp = (sum(a["xp"] for a in achs if a["unlocked"]) + 2 * len(txns) + 15 * payments
          + 10 * len(m["_scores"]) + 5 * active_days)
    level, into, need = level_for(xp)
    stage = [name for lvl, name in STAGES if level >= lvl][-1]
    u = m["_util"]
    missed = any(k.startswith("missed:") for k, _ in credit.reminders(accounts, today, txns=txns))
    mood = "worried" if missed or (u is not None and u > 0.5) else "okay" if u is not None and u > 0.3 else "happy"
    by_id = {a["id"]: a for a in achs}
    current = next((i for i in PATH if not by_id[i]["unlocked"]), None)
    streak, active_today = daily_streak(today)
    return {"xp": xp, "level": level, "xp_into": into, "xp_need": need, "stage": stage, "mood": mood,
            "daily_streak": streak, "active_today": active_today, "ontime_months": m["ontime"],
            "achievements": achs, "unlocked": sum(a["unlocked"] for a in achs),
            "path": [{**by_id[i], "current": i == current} for i in PATH]}


def new_unlocks(accounts, txns, today=None):
    """Achievements unlocked since last check (marks them seen). The first ever run is silent."""
    first_run = not db.kv_get("ach:init")
    fresh = []
    for a in state(accounts, txns, today)["achievements"]:
        if a["unlocked"] and not db.kv_get("ach:" + a["id"]):
            db.kv_set("ach:" + a["id"], (today or date.today()).isoformat())
            fresh.append(a)
    db.kv_set("ach:init", 1)
    return [] if first_run else fresh


def unlock_text(achs):
    return "\n".join(f"🏆 Achievement unlocked: {a['title']} (+{a['xp']} XP)" for a in achs)
