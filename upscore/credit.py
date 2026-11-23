"""Deterministic credit engine. Every number the agent quotes comes from here, never from the LLM.

Based on the public FICO factor weights: payment history 35%, amounts owed (utilization) 30%,
length of history 15%, new credit 10%, mix 10%.
"""
import calendar
from collections import defaultdict
from datetime import date, timedelta

GOOD_UTIL = 0.10   # report under 10% for best scores
BAD_UTIL = 0.30


def next_day_of_month(day, today):
    """Next date (>= today) falling on `day` of a month, clamped to short months."""
    if not day:
        return None
    y, m = today.year, today.month
    for _ in range(2):
        d = date(y, m, min(day, calendar.monthrange(y, m)[1]))
        if d >= today:
            return d
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return d


def utilization(accounts):
    cards = [a for a in accounts if a["kind"] == "credit" and a.get("credit_limit")]
    total_bal = sum(max(a["balance"], 0) for a in cards)
    total_lim = sum(a["credit_limit"] for a in cards)
    per = {a["name"]: round(max(a["balance"], 0) / a["credit_limit"], 4) for a in cards}
    return {"overall": round(total_bal / total_lim, 4) if total_lim else None,
            "balance": round(total_bal, 2), "limit": round(total_lim, 2), "per_card": per}


def subscriptions(txns, today):
    """Merchants flagged recurring (or charged in 2+ distinct months for similar amounts) in the last 120 days."""
    since = (today - timedelta(days=120)).isoformat()
    by_merchant = defaultdict(list)
    for t in txns:
        if t["kind"] == "purchase" and t["date"] >= since:
            by_merchant[t["merchant"].strip().lower()].append(t)
    subs = []
    for key, ts in by_merchant.items():
        months = {t["date"][:7] for t in ts}
        amounts = [t["amount"] for t in ts]
        similar = max(amounts) - min(amounts) <= max(2.0, 0.1 * max(amounts))
        if any(t["recurring"] for t in ts) or (len(months) >= 2 and similar):
            latest = max(ts, key=lambda t: t["date"])
            subs.append({"merchant": latest["merchant"], "monthly": round(latest["amount"], 2),
                         "category": latest.get("category"), "last_charged": latest["date"]})
    return sorted(subs, key=lambda s: -s["monthly"])


def monthly_income(txns, today):
    since = (today - timedelta(days=30)).isoformat()
    return round(sum(t["amount"] for t in txns if t["kind"] == "income" and t["date"] >= since), 2)


def plan(accounts, txns, today=None):
    """Prioritized, dated actions. Lower priority number = do first."""
    today = today or date.today()
    acts = []
    for a in accounts:
        if a["kind"] != "credit" or a["balance"] <= 0:
            continue
        due = next_day_of_month(a.get("due_day"), today)
        close = next_day_of_month(a.get("statement_day"), today)
        lim = a.get("credit_limit")

        if due and (due - today).days <= 7:
            pay = a.get("min_payment") or min(a["balance"], max(25.0, round(a["balance"] * 0.02, 2)))
            acts.append({"priority": 1, "type": "due", "account": a["name"], "date": due.isoformat(),
                         "amount": round(pay, 2),
                         "title": f"Pay at least ${pay:,.2f} on {a['name']} by {due:%b %d}",
                         "why": "A payment 30+ days late can drop a score 60-110 points. Payment history is 35% of FICO."})
        if not a.get("autopay"):
            acts.append({"priority": 4, "type": "autopay", "account": a["name"],
                         "title": f"Turn on autopay for the minimum on {a['name']}",
                         "why": "A safety net so a busy week never becomes a late payment."})
        if lim:
            u = a["balance"] / lim
            if u > GOOD_UTIL:
                target = round(a["balance"] - lim * 0.09, 2)
                when = close or due
                acts.append({"priority": 2 if u > BAD_UTIL else 3, "type": "utilization", "account": a["name"],
                             "date": when.isoformat() if when else None, "amount": target, "utilization": round(u, 4),
                             "title": (f"Pay ${target:,.2f} on {a['name']}"
                                       + (f" before the statement closes {when:%b %d}" if close else "")
                                       + f" to report under 10% (now {u:.0%})"),
                             "why": "Bureaus see the balance on your statement date, not your due date. Utilization is 30% of FICO."})

    u = utilization(accounts)
    if u["overall"] is not None and u["overall"] > BAD_UTIL:
        acts.append({"priority": 2, "type": "overall_util", "utilization": u["overall"],
                     "title": f"Overall utilization is {u['overall']:.0%}. Aim for under 30%, ideally under 10%",
                     "why": "Overall and per-card utilization both count."})

    subs = subscriptions(txns, today)
    total = round(sum(s["monthly"] for s in subs), 2)
    review = [s for s in subs if s["monthly"] >= 15]
    cats = defaultdict(list)
    for s in subs:
        if s.get("category"):
            cats[s["category"].lower()].append(s["merchant"])
    dupes = {c: m for c, m in cats.items() if len(m) > 1}
    if review or dupes:
        acts.append({"priority": 5, "type": "subscriptions", "amount": total,
                     "title": f"Review subscriptions: ${total:,.2f}/mo"
                              + (f", overlapping {', '.join(dupes)}" if dupes else ""),
                     "why": "Cancelling unused subscriptions frees up cash to pay balances down.",
                     "items": [s["merchant"] for s in review], "overlap": dupes})

    income = monthly_income(txns, today)
    mins = sum(a.get("min_payment") or 0 for a in accounts if a["kind"] == "credit")
    if income and mins + total > income * 0.5:
        acts.append({"priority": 3, "type": "cashflow",
                     "title": f"Minimums + subscriptions (${mins + total:,.0f}) exceed half your income (${income:,.0f})",
                     "why": "That leaves little buffer, and a short month risks a missed payment."})

    acts.append({"priority": 9, "type": "keep_old", "title": "Keep your oldest cards open, even if unused",
                 "why": "Closing them shortens your history (15%) and cuts available credit."})
    return sorted(acts, key=lambda x: (x["priority"], x.get("date") or "9999"))


def reminders(accounts, today=None, window=3):
    """(key, text) pairs for upcoming due and statement dates. The key dedupes so each is sent once."""
    today = today or date.today()
    out = []
    for a in accounts:
        if a["kind"] != "credit" or a["balance"] <= 0:
            continue
        due = next_day_of_month(a.get("due_day"), today)
        if due and (due - today).days <= window:
            out.append((f"due:{a['name']}:{due}",
                        f"⏰ {a['name']} payment due {due:%a %b %d}. Balance ${a['balance']:,.2f}. Pay at least the minimum to protect your score."))
        close = next_day_of_month(a.get("statement_day"), today)
        lim = a.get("credit_limit")
        if close and lim and (close - today).days <= window and a["balance"] / lim > GOOD_UTIL:
            target = a["balance"] - lim * 0.09
            out.append((f"close:{a['name']}:{close}",
                        f"📉 {a['name']} statement closes {close:%a %b %d} at {a['balance'] / lim:.0%} utilization. "
                        f"Pay ${target:,.2f} before then so it reports under 10%."))
    return out


def summary(accounts, txns, today=None):
    today = today or date.today()
    since = (today - timedelta(days=30)).isoformat()
    spend = defaultdict(float)
    for t in txns:
        if t["kind"] == "purchase" and t["date"] >= since:
            spend[t.get("category") or "other"] += t["amount"]
    return {"today": today.isoformat(), "utilization": utilization(accounts),
            "subscriptions": subscriptions(txns, today), "income_30d": monthly_income(txns, today),
            "spend_30d": round(sum(spend.values()), 2),
            "spend_by_category_30d": {k: round(v, 2) for k, v in sorted(spend.items(), key=lambda kv: -kv[1])},
            "accounts": [{**a, "next_due": str(next_day_of_month(a.get("due_day"), today) or ""),
                          "next_statement": str(next_day_of_month(a.get("statement_day"), today) or "")}
                         for a in accounts],
            "plan": plan(accounts, txns, today)}
