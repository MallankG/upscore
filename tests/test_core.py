"""Run: python3 -m unittest discover tests"""
import os
import unittest
from datetime import date

os.environ["UPSCORE_DB"] = ":memory:"
from upscore import agent, credit, db  # noqa: E402

T = date(2026, 11, 23)


def card(**kw):
    base = dict(name="Chase", kind="credit", credit_limit=2000, balance=1450, statement_day=3, due_day=28,
                min_payment=None, autopay=0)
    return {**base, **kw}


class CreditTest(unittest.TestCase):
    def test_next_day_clamps_and_rolls(self):
        self.assertEqual(credit.next_day_of_month(28, T), date(2026, 11, 28))
        self.assertEqual(credit.next_day_of_month(3, T), date(2026, 12, 3))
        self.assertEqual(credit.next_day_of_month(31, date(2027, 2, 1)), date(2027, 2, 28))
        self.assertEqual(credit.next_day_of_month(23, T), T)

    def test_plan_high_utilization(self):
        p = credit.plan([card()], [], T)
        due = next(a for a in p if a["type"] == "due")
        self.assertEqual(due["date"], "2026-11-28")
        u = next(a for a in p if a["type"] == "utilization")
        self.assertEqual(u["priority"], 2)
        self.assertAlmostEqual(u["amount"], 1450 - 180)  # down to 9% of 2000
        self.assertEqual(u["date"], "2026-12-03")
        self.assertEqual(p[0]["type"], "due")

    def test_low_util_no_util_action(self):
        p = credit.plan([card(balance=100, due_day=15)], [], T)
        self.assertFalse(any(a["type"] in ("utilization", "due") for a in p))

    def test_subscriptions_detected(self):
        tx = [dict(date=d, amount=15.49, merchant="Netflix", kind="purchase", recurring=0, category="streaming")
              for d in ("2026-09-05", "2026-10-05")]
        tx.append(dict(date="2026-11-01", amount=11.99, merchant="Hulu", kind="purchase", recurring=1, category="streaming"))
        tx.append(dict(date="2026-11-02", amount=84.12, merchant="Whole Foods", kind="purchase", recurring=0, category="groceries"))
        subs = credit.subscriptions(tx, T)
        self.assertEqual([s["merchant"] for s in subs], ["Netflix", "Hulu"])
        s = next(a for a in credit.plan([], tx, T) if a["type"] == "subscriptions")
        self.assertIn("streaming", s["overlap"])

    def test_reminders(self):
        keys = [k for k, _ in credit.reminders([card()], date(2026, 11, 30))]
        self.assertEqual(keys, ["missed:Chase:2026-11-28", "close:Chase:2026-12-03"])  # no payment logged
        paid = [dict(date="2026-11-20", amount=50, merchant="Chase", kind="payment", account="Chase", recurring=0)]
        keys = [k for k, _ in credit.reminders([card()], date(2026, 11, 30), txns=paid)]
        self.assertEqual(keys, ["close:Chase:2026-12-03"])

    def test_simulate_paydown(self):
        sim = credit.simulate([card(), card(name="Amex", credit_limit=5000, balance=0)], {"chase": 1270}, "642")
        self.assertAlmostEqual(sim["before"]["per_card"]["Chase"], 0.725)
        self.assertAlmostEqual(sim["after"]["per_card"]["Chase"], 0.09)
        lo, hi = sim["est_points"]
        self.assertTrue(0 < lo <= hi)
        self.assertEqual(sim["est_score"], [642 + lo, 642 + hi])
        self.assertIn("error", credit.simulate([card()], {"Discover": 10}))

    def test_streaks_and_checkin(self):
        pays = [dict(date=d, amount=100, merchant="Chase", kind="payment", account="Chase", recurring=0)
                for d in ("2026-09-20", "2026-10-25", "2026-11-01")]  # cycles ending Sep 28, Oct 28 (Nov 1 is next cycle)
        self.assertEqual(credit.streaks([card()], pays, T), {"Chase": 2})
        pays.append(dict(date="2026-11-22", amount=40, merchant="Shell", kind="purchase", account="Chase", recurring=0))
        msg = credit.checkin([card()], pays, T)
        self.assertIn("$40.00 this week", msg)
        self.assertIn("Chase due", msg)
        self.assertIn("streak: 2", msg)


class DbTest(unittest.TestCase):
    def test_txn_moves_balance_and_dedupes(self):
        db.upsert_account("Amex", credit_limit=5000, balance=0)
        db.add_txn(100, "Target", account="Amex", date_="2026-11-20")
        self.assertTrue(db.add_txn(100, "Target", account="Amex", date_="2026-11-20")["duplicate"])
        db.add_txn(40, "Amex", kind="payment", account="Amex", date_="2026-11-21")
        self.assertEqual(db.q("SELECT balance FROM accounts WHERE name='amex'")[0]["balance"], 60)


class AgentLoopTest(unittest.TestCase):
    def test_tool_call_then_reply(self):
        script = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {
                "name": "upsert_account",
                "arguments": '{"name":"Chase","credit_limit":2000,"balance":1450,"statement_day":3,"due_day":28}'}}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "2", "type": "function", "function": {
                "name": "get_plan", "arguments": "{}"}}]},
            {"role": "assistant", "content": "Pay $1,270 before Dec 3."},
        ]
        seen = []
        orig = agent.llm
        agent.llm = lambda msgs, model=None: (seen.append(msgs[-1]), script.pop(0))[1]
        try:
            self.assertEqual(agent.respond("test", "chase limit 2k bal 1450", "web"), "Pay $1,270 before Dec 3.")
        finally:
            agent.llm = orig
        self.assertIn("utilization", seen[-1]["content"])  # plan output reached the model
        self.assertEqual(db.q("SELECT balance FROM accounts WHERE name='Chase'")[0]["balance"], 1450)


if __name__ == "__main__":
    unittest.main()
