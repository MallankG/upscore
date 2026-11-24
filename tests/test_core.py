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


class GamifyTest(unittest.TestCase):
    def test_levels(self):
        from upscore import gamify
        self.assertEqual(gamify.level_for(0), (1, 0, 100))
        self.assertEqual(gamify.level_for(100), (2, 0, 150))
        self.assertEqual(gamify.level_for(260), (3, 10, 200))

    def test_daily_streak_survives_until_tomorrow(self):
        from upscore import gamify
        day = date(2025, 3, 10)  # far from real "today", which other tests' agent turns mark active
        for d in ("2025-03-07", "2025-03-08", "2025-03-09"):
            db.kv_set("day:" + d, 1)
        self.assertEqual(gamify.daily_streak(day), (3, False))      # today not done yet: still 3
        gamify.record_activity(day)
        self.assertEqual(gamify.daily_streak(day), (4, True))
        self.assertEqual(gamify.daily_streak(date(2025, 3, 12))[0], 0)  # skipped the 11th

    def test_state_stage_mood_and_path(self):
        from upscore import gamify
        st = gamify.state([card()], [], T)
        self.assertEqual(st["mood"], "worried")  # 72% utilization
        self.assertEqual(st["path"][0]["id"], "card_added")
        cur = [p for p in st["path"] if p["current"]]
        self.assertEqual(len(cur), 1)
        self.assertIn(st["stage"], [n for _, n in gamify.STAGES])


class RewardsTest(unittest.TestCase):
    def setUp(self):
        from upscore import rewards
        self.r = rewards
        self.cards = [card(name="Freedom", balance=100), card(name="Amex Gold", credit_limit=5000, balance=0),
                      card(name="Maxed", credit_limit=1000, balance=280)]
        self.rules = [dict(card="Freedom", category="everything", rate=1.5, start=None, end=None, cap=None),
                      dict(card="Freedom", category="gas", rate=5, start="2026-10-01", end="2026-12-31", cap=1500),
                      dict(card="Amex Gold", category="dining", rate=4, start=None, end=None, cap=None),
                      dict(card="Amex Gold", category="groceries", rate=4, start=None, end=None, cap=None),
                      dict(card="Maxed", category="groceries", rate=6, start=None, end=None, cap=None)]

    def test_normalize(self):
        self.assertEqual([self.r.normalize(c) for c in ("Restaurants", "Gas Stations", "amazon.com", "coffee", "")],
                         ["dining", "gas", "online shopping", "dining", "other"])

    def test_rotating_category_only_in_window(self):
        self.assertEqual(self.r.best_card("gas", self.cards, self.rules, T)["pick"]["card"], "Freedom")
        jan = self.r.best_card("gas", self.cards, self.rules, date(2027, 1, 5))["pick"]
        self.assertEqual((jan["card"], jan["rate"]), ("Freedom", 1.5))  # back to base rate

    def test_utilization_guard_beats_higher_rate(self):
        b = self.r.best_card("groceries", self.cards, self.rules, T, amount=100)
        self.assertEqual(b["ranked"][0]["card"], "Maxed")  # 6% but would hit 38%
        self.assertEqual(b["pick"]["card"], "Amex Gold")
        self.assertIn("utilization", b["note"])

    def test_missed_rewards_tip(self):
        t = dict(kind="purchase", account="Freedom", category="dining", amount=80, date=T.isoformat())
        m = self.r.missed(t, self.cards, self.rules)
        self.assertEqual((m["better_card"], m["extra"]), ("Amex Gold", 2.0))  # (4% - 1.5%) of $80
        self.assertIsNone(self.r.missed({**t, "account": "Amex Gold"}, self.cards, self.rules))
        sheet = {x["category"]: x["card"] for x in self.r.cheat_sheet(self.cards, self.rules, T)}
        self.assertEqual((sheet["dining"], sheet["gas"]), ("Amex Gold", "Freedom"))


class ProviderTest(unittest.TestCase):
    def test_provider_flag_presets_and_overrides(self):
        import importlib
        env = {k: os.environ.get(k) for k in ("LLM_PROVIDER", "LLM_MODEL")}
        try:
            os.environ["LLM_PROVIDER"] = "ollama"
            os.environ.pop("LLM_MODEL", None)
            importlib.reload(agent)
            self.assertEqual((agent.LLM_BASE_URL, agent.LLM_MODEL, agent.LLM_REASONING_EFFORT),
                             ("http://localhost:11434/v1", "qwen3.5:4b", "none"))
            os.environ["LLM_MODEL"] = "gemma4:e4b"
            importlib.reload(agent)
            self.assertEqual(agent.LLM_MODEL, "gemma4:e4b")
            os.environ["LLM_PROVIDER"] = "meta"
            os.environ.pop("LLM_MODEL")
            importlib.reload(agent)
            self.assertEqual((agent.LLM_MODEL, agent.LLM_FAST_MODEL), ("muse-spark-1.3", "muse-ember-1.0"))
        finally:
            for k, v in env.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            importlib.reload(agent)


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
