"""Demo data: "Maya", a fictional user whose data exercises every feature.
    UPSCORE_DB=/tmp/demo.db python3 -m upscore.seed

What each piece demonstrates:
- Chase Freedom at 73% (statement closes the 3rd, due the 28th) -> the dated "$1,270 before Dec 3" quest,
  card-health red bar, worried Pip, the 30% guard pausing its 5% rotating categories.
- Amex Blue at 45%, Discover it at 12% -> overall utilization, which-card-where across three cards.
- Three months of on-time payments on every card -> on-time streak 3 (Clean quarter unlocked).
- Netflix + Hulu + Disney+ (overlapping streaming), Equinox, Spotify -> money leaks + cancel helper.
- Paychecks -> cash-flow check; score history 601 -> 642 toward "720 by June" -> chart + milestones.
- 9-day check-in streak, one statement upload -> Week warrior, Paper trail.
"""
from datetime import date, timedelta

from . import db

T = date.today()


def ago(n):
    return (T - timedelta(days=n)).isoformat()


CARDS = [  # name, limit, statement day, due day, minimum, autopay, balance now
    ("Chase Freedom", 2000, 3, 28, 35, False, 1450.00),
    ("Amex Blue", 1500, 18, 12, 40, True, 680.00),
    ("Discover it", 1000, 25, 20, 25, True, 120.00),
]
for name, lim, st, due, mn, auto, _ in CARDS:
    db.upsert_account(name, credit_limit=lim, balance=0, statement_day=st, due_day=due, min_payment=mn, autopay=auto)
db.upsert_account("Checking", kind="checking", balance=2140)

# ~4 months of everyday spending (days ago, amount, merchant, category, card, recurring)
SPEND = [
    (2, 84.12, "Whole Foods", "groceries", "Chase Freedom", 0), (3, 180.60, "Chipotle catering", "dining", "Amex Blue", 0),
    (4, 412.00, "Trader Joe's", "groceries", "Amex Blue", 0), (5, 15.49, "Netflix", "streaming", "Chase Freedom", 1),
    (6, 11.99, "Hulu", "streaming", "Chase Freedom", 1), (7, 13.99, "Disney+", "streaming", "Discover it", 1),
    (8, 37.41, "CVS", "drugstore", "Amex Blue", 0), (9, 642.00, "Delta Air Lines", "travel", "Chase Freedom", 0),
    (10, 10.99, "Spotify", "streaming", "Amex Blue", 1), (11, 23.80, "Uber", "transit", "Discover it", 0),
    (12, 520.30, "Best Buy", "online shopping", "Chase Freedom", 0), (13, 48.20, "Blue Bottle Coffee", "dining", "Discover it", 0),
    (14, 176.10, "Shell", "gas", "Chase Freedom", 0), (16, 34.50, "Sweetgreen", "dining", "Discover it", 0),
    (20, 49.99, "Equinox", "entertainment", "Amex Blue", 1), (35, 15.49, "Netflix", "streaming", "Chase Freedom", 1),
    (36, 11.99, "Hulu", "streaming", "Chase Freedom", 1), (37, 13.99, "Disney+", "streaming", "Discover it", 1),
    (40, 10.99, "Spotify", "streaming", "Amex Blue", 1), (42, 96.40, "Safeway", "groceries", "Amex Blue", 0),
    (45, 62.15, "Chevron", "gas", "Discover it", 0), (50, 49.99, "Equinox", "entertainment", "Amex Blue", 1),
    (52, 128.00, "Amazon", "online shopping", "Chase Freedom", 0), (58, 74.30, "Olive Garden", "dining", "Amex Blue", 0),
    (65, 15.49, "Netflix", "streaming", "Chase Freedom", 1), (66, 11.99, "Hulu", "streaming", "Chase Freedom", 1),
    (70, 10.99, "Spotify", "streaming", "Amex Blue", 1), (75, 211.45, "Costco", "groceries", "Amex Blue", 0),
    (80, 49.99, "Equinox", "entertainment", "Amex Blue", 1), (88, 39.60, "Lyft", "transit", "Discover it", 0),
    (95, 15.49, "Netflix", "streaming", "Chase Freedom", 1), (100, 10.99, "Spotify", "streaming", "Amex Blue", 1),
]
for days, amt, merchant, cat, card, rec in SPEND:
    db.add_txn(amt, merchant, "purchase", ago(days), cat, card, rec, "seed")

# paychecks (biweekly)
for days in (7, 21, 35, 49, 63, 77):
    db.add_txn(1850, "Acme Corp payroll", "income", ago(days), "other", "Checking", source="seed")


def last_due_dates(due_day, n):
    out, y, m = [], T.year, T.month
    while len(out) < n:
        try:
            dd = date(y, m, due_day)
        except ValueError:
            dd = date(y, m, 28)
        if dd < T:
            out.append(dd)
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


# three on-time payments per card, each a week before its due date -> on-time streak 3
for name, lim, st, due, mn, auto, _ in CARDS:
    for dd in last_due_dates(due, 3):
        db.add_txn(round(lim * 0.3, 2), name, "payment", (dd - timedelta(days=7)).isoformat(), account=name, source="seed")

# balances as they stand today (payments above are history)
for name, *_rest, bal in CARDS:
    db.run("UPDATE accounts SET balance=? WHERE name=?", (bal, name))

for days, score in [(150, 601), (120, 612), (90, 620), (60, 629), (30, 636), (0, 642)]:
    db.log_score(score, ago(days))
db.kv_set("profile:credit_score", "642")
db.kv_set("profile:goal", "720 by June")
for n in range(1, 10):  # 9-day check-in streak, still alive today
    db.kv_set("day:" + ago(n), 1)
db.kv_set("count:uploads", 1)

# reward rules, including this quarter's rotating 5% (Chase Freedom Flex Q4: groceries + dining, per Tavily)
qs = date(T.year, (T.month - 1) // 3 * 3 + 1, 1)
qe = date(qs.year + (qs.month + 2) // 12, (qs.month + 2) % 12 + 1, 1) - timedelta(days=1)
for card, cat, rate, s, e in [
    ("Chase Freedom", "everything", 1, None, None), ("Chase Freedom", "groceries", 5, qs, qe), ("Chase Freedom", "dining", 5, qs, qe),
    ("Amex Blue", "everything", 1, None, None), ("Amex Blue", "groceries", 3, None, None),
    ("Amex Blue", "streaming", 3, None, None), ("Amex Blue", "gas", 2, None, None),
    ("Discover it", "everything", 1, None, None), ("Discover it", "online shopping", 5, qs, qe), ("Discover it", "transit", 2, None, None),
]:
    db.set_reward(card, cat, rate, s and s.isoformat(), e and e.isoformat())

from . import agent, gamify  # noqa: E402
gamify.new_unlocks(*agent.load())  # first run is silent: what Maya already earned isn't "new"
print("seeded Maya ->", db.DB_PATH)
