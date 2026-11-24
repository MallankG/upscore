"""Demo data for screenshots/videos:  UPSCORE_DB=/tmp/demo.db python3 -m upscore.seed"""
from datetime import date, timedelta

from . import db

d = lambda n: (date.today() - timedelta(days=n)).isoformat()  # noqa: E731

db.upsert_account("Chase Freedom", credit_limit=2000, balance=0, statement_day=3, due_day=28, min_payment=35)
db.upsert_account("Amex Blue", credit_limit=5000, balance=0, statement_day=18, due_day=12, autopay=True)
db.upsert_account("Chase Checking", kind="checking", balance=1820)
for amt, m, cat, acct, days, rec in [
    (84.12, "Whole Foods", "groceries", "Chase Freedom", 2, 0), (15.49, "Netflix", "streaming", "Chase Freedom", 5, 1),
    (11.99, "Hulu", "streaming", "Chase Freedom", 6, 1), (642.00, "Delta Air Lines", "travel", "Chase Freedom", 9, 0),
    (520.30, "Best Buy", "electronics", "Chase Freedom", 12, 0), (176.10, "Shell", "gas", "Chase Freedom", 14, 0),
    (210.00, "Trader Joe's", "groceries", "Amex Blue", 4, 0), (10.99, "Spotify", "music", "Amex Blue", 8, 1),
    (49.99, "Equinox", "fitness", "Amex Blue", 20, 1), (65.40, "Chipotle", "dining", "Amex Blue", 3, 0),
]:
    db.add_txn(amt, m, "purchase", d(days), cat, acct, rec, "seed")
db.add_txn(2400, "Acme Corp payroll", "income", d(7), "salary", "Chase Checking", source="seed")
for months_ago, acct, amt in [(3, "Chase Freedom", 400), (2, "Chase Freedom", 350), (1, "Chase Freedom", 500),
                              (3, "Amex Blue", 120), (2, "Amex Blue", 90), (1, "Amex Blue", 150)]:
    db.add_txn(amt, acct, "payment", d(30 * months_ago + 8), account=acct, source="seed")  # paid ~a week before due
db.run("UPDATE accounts SET balance=0")  # payments above are history; reset balances, then re-apply recent buys
for t in db.q("SELECT amount, account FROM txns WHERE kind='purchase'"):
    db.run("UPDATE accounts SET balance=balance+? WHERE name=?", (t["amount"], t["account"]))
for days, score in [(150, 598), (120, 611), (90, 619), (60, 628), (30, 636), (0, 642)]:
    db.log_score(score, d(days))
db.kv_set("profile:credit_score", "642")
db.kv_set("profile:goal", "720 by June")
print("seeded", db.DB_PATH)
