"""Card rewards optimizer: which card to use where, including rotating quarterly categories.
Pure logic over reward rules; the agent learns rules from the user or a web search and stores them."""
from datetime import date

CATEGORIES = ["groceries", "dining", "gas", "travel", "streaming", "drugstore", "online shopping",
              "entertainment", "transit", "utilities", "rent", "other"]
EVERYTHING = "everything"  # a card's base rate on all purchases
ALIASES = {
    "grocery": "groceries", "supermarket": "groceries", "supermarkets": "groceries", "food": "groceries",
    "restaurant": "dining", "restaurants": "dining", "food delivery": "dining", "takeout": "dining", "coffee": "dining",
    "gas station": "gas", "gas stations": "gas", "fuel": "gas", "ev charging": "gas",
    "airline": "travel", "airlines": "travel", "hotel": "travel", "hotels": "travel", "flights": "travel",
    "streaming services": "streaming", "music": "streaming", "subscriptions": "streaming",
    "pharmacy": "drugstore", "drugstores": "drugstore", "pharmacies": "drugstore",
    "amazon": "online shopping", "online": "online shopping", "online retail": "online shopping", "shopping": "online shopping",
    "movies": "entertainment", "concerts": "entertainment", "rideshare": "transit", "uber": "transit", "lyft": "transit",
    "all": EVERYTHING, "all purchases": EVERYTHING, "everything else": EVERYTHING, "other purchases": EVERYTHING,
    "salary": "other", "electronics": "online shopping", "fitness": "entertainment",
}
DEFAULT_RATE = 1.0  # % back assumed for a card we know nothing about


def normalize(cat):
    c = (cat or "").strip().lower()
    if c in CATEGORIES or c == EVERYTHING:
        return c
    if c in ALIASES:
        return ALIASES[c]
    for k, v in ALIASES.items():
        if k in c:
            return v
    return "other"


def active(rule, today):
    return (not rule.get("start") or rule["start"] <= today.isoformat()) and \
           (not rule.get("end") or today.isoformat() <= rule["end"])


def rate_for(card, category, rules, today):
    """(% back, rule) for spending `category` on `card` today. Category rules beat the card's base rate."""
    cat = normalize(category)
    mine = [r for r in rules if r["card"].lower() == card.lower() and active(r, today)]
    hits = [r for r in mine if r["category"] == cat] or [r for r in mine if r["category"] == EVERYTHING]
    if not hits:
        return DEFAULT_RATE, None
    best = max(hits, key=lambda r: r["rate"])
    return best["rate"], best


def best_card(category, accounts, rules, today=None, amount=0.0):
    """Rank credit cards for a purchase. A card is skipped as the pick if this purchase would push it over 30%."""
    today = today or date.today()
    ranked = []
    for a in accounts:
        if a["kind"] != "credit":
            continue
        rate, rule = rate_for(a["name"], category, rules, today)
        lim = a.get("credit_limit")
        util_after = (a["balance"] + amount) / lim if lim else None
        ranked.append({"card": a["name"], "rate": rate, "util_after": round(util_after, 4) if util_after is not None else None,
                       "rotating": bool(rule and rule.get("end")), "cap": rule.get("cap") if rule else None,
                       "ends": rule.get("end") if rule else None})
    ranked.sort(key=lambda r: -r["rate"])
    ok = [r for r in ranked if r["util_after"] is None or r["util_after"] <= 0.30]
    pick = ok[0] if ok else (ranked[0] if ranked else None)
    note = None
    if pick and ranked and pick is not ranked[0]:
        note = (f"{ranked[0]['card']} pays more ({ranked[0]['rate']:g}%) but this would push it to "
                f"{ranked[0]['util_after']:.0%} utilization, which costs more score than the rewards are worth.")
    return {"category": normalize(category), "pick": pick, "ranked": ranked, "note": note}


def missed(txn, accounts, rules, today=None):
    """Extra cash back the user would have earned with the best card, or None if they used it."""
    if txn.get("kind") != "purchase" or not txn.get("account"):
        return None
    today = today or date.fromisoformat(txn["date"])
    used, _ = rate_for(txn["account"], txn.get("category"), rules, today)
    b = best_card(txn.get("category"), accounts, rules, today)
    if not b["pick"] or b["pick"]["card"].lower() == txn["account"].lower() or b["pick"]["rate"] <= used:
        return None
    extra = round(txn["amount"] * (b["pick"]["rate"] - used) / 100, 2)
    return {"better_card": b["pick"]["card"], "rate": b["pick"]["rate"], "used_rate": used, "extra": extra}


def optimal_count(txns, accounts, rules):
    """Purchases made on the best card (or a tie) — powers the Reward hunter achievement."""
    n = 0
    for t in txns:
        if t.get("kind") == "purchase" and t.get("account") and any(
                a["name"].lower() == t["account"].lower() and a["kind"] == "credit" for a in accounts):
            if missed(t, accounts, rules) is None:
                n += 1
    return n


def cheat_sheet(accounts, rules, today=None):
    """Best card per category today, for the dashboard's 'which card where'."""
    today = today or date.today()
    if not any(a["kind"] == "credit" for a in accounts):
        return []
    out = []
    for cat in CATEGORIES[:-1]:
        b = best_card(cat, accounts, rules, today)
        if b["pick"]:
            top = b["ranked"][0]
            out.append({"category": cat, "card": b["pick"]["card"], "rate": b["pick"]["rate"],
                        "rotating": b["pick"]["rotating"], "ends": b["pick"]["ends"],
                        # the higher-paying card skipped because it's over 30% utilization
                        "paused": {"card": top["card"], "rate": top["rate"], "util": top["util_after"]}
                        if top is not b["pick"] and top["rate"] > b["pick"]["rate"] else None})
    return out
