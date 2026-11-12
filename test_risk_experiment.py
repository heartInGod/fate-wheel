#!/usr/bin/env python3
import copy
import contextlib
import datetime as dt
import io
import json
import unittest
from unittest.mock import patch, mock_open
import portfolio_risk as pr
import risk_experiment as e


def bars(prices):
    start = dt.date(2026, 1, 1)
    return [{"date": (start + dt.timedelta(days=i)).isoformat(), "open": p, "close": p,
             "high": p + 0.01, "low": p - 0.01, "volume": 1000} for i, p in enumerate(prices)]


class RiskExperimentTests(unittest.TestCase):
    def setUp(self):
        self.p = pr.load_policy()
        self.p["experiments"]["trend"].update(ma_days=3, confirm_days=2, recover_days=2)
        self.p["experiments"]["time"].update(review_days=3)

    def run_strategy(self, prices, strategy):
        return e.simulate("600000", bars(prices), strategy, self.p)

    def test_next_open_execution(self):
        r = self.run_strategy([10, 10, 10, 9, 8, 7, 7, 7, 7], "trend")
        sells = [t for t in r["trades"] if t["side"] < 0]
        self.assertTrue(sells)
        self.assertGreater(sells[0]["date"], sells[0]["signal_date"])
        self.assertAlmostEqual(sells[0]["price"], 7 * 0.999)

    def test_hold_only_has_one_buy(self):
        r = self.run_strategy([10] * 15, "hold")
        self.assertEqual(r["trade_count"], 1)
        self.assertGreater(r["fee_yuan"], 0)
        self.assertLess(r["net_return_frac"], 0)

    def test_profit_only_once_per_cycle(self):
        r = self.run_strategy([10, 10, 10, 10, 15, 14, 12, 11, 10, 9, 8], "profit")
        self.assertEqual(r["sell_count"], 1)
        self.assertGreater(r["history"][-1]["shares"], 0)

    def test_time_trim_only_once(self):
        r = self.run_strategy([10] * 20, "time")
        self.assertEqual(r["sell_count"], 1)
        self.assertEqual(r["trades"][-1]["reason"], "time_review_trim")

    def test_blocked_execution_keeps_pending(self):
        bs = bars([10, 10, 10, 9, 8, 7, 6, 5])
        for b in bs[5:]:
            b["tradeable"] = False
        r = e.simulate("600000", bs, "trend", self.p)
        self.assertEqual(r["sell_count"], 0)
        self.assertIsNotNone(r["pending_at_end"])
        self.assertGreater(r["blocked_execution_days"], 0)

    def test_zero_volume_cannot_fill(self):
        self.assertTrue(e.blocked(dict(bars([10])[0], volume=0), None, -1, "600000"))

    def test_locked_limit_down_block(self):
        before = bars([10])[0]
        b = dict(bars([9])[0], high=9, low=9)
        self.assertTrue(e.blocked(b, before, -1, "600000"))

    def test_future_prices_cannot_change_past_trades(self):
        first = bars([10, 10, 10, 9, 8, 7, 7, 7, 7])
        second = first + bars([100, 100])
        second[-2]["date"] = "2026-02-01"
        second[-1]["date"] = "2026-02-02"
        a = e.simulate("600000", first, "trend", self.p)
        b = e.simulate("600000", second, "trend", self.p)
        prior = [t for t in b["trades"] if t["date"] <= first[-1]["date"]]
        self.assertEqual(a["trades"], prior)

    def test_bad_bar_rejected(self):
        with self.assertRaises(ValueError):
            e.normalize_bars([dict(bars([10])[0], high=9)])

    def test_future_bar_filtered(self):
        bs = bars([10, 11, 12])
        self.assertEqual(len(e.normalize_bars(bs, "2026-01-02")), 2)

    def test_blocked_trend_defense_cancelled_after_recovery(self):
        bs = bars([10, 10, 10, 9, 8, 9, 10, 11, 11])
        for b in bs[5:8]:
            b["tradeable"] = False
        r = e.simulate("600000", bs, "trend", self.p)
        self.assertEqual(r["sell_count"], 0)
        self.assertIsNone(r["pending_at_end"])
        self.assertTrue(r["cancelled_plans"])

    def test_hard_protection_tightens_existing_blocked_plan(self):
        self.p["experiments"]["profit"]["exit_fraction"] = 0.8
        bs = bars([10, 10, 10, 10, 15, 14, 13, 12, 11, 10, 9])
        for b in bs[7:9]:
            b["tradeable"] = False
        r = e.simulate("600000", bs, "combined", self.p)
        self.assertTrue(any(t["reason"] == "profit_protect" for t in r["trades"]))
        self.assertLessEqual(r["history"][-1]["shares"], 2000)

    def test_invalid_experiment_settings_rejected(self):
        cases = [("trend", "ma_days", 0), ("trend", "confirm_days", 1.5),
                 ("trend", "recover_days", True), ("profit", "exit_fraction", 1.1),
                 ("time", "review_days", "120"), ("profit", "trailing_drop_frac", float("nan"))]
        for section, key, value in cases:
            with self.subTest(section=section, key=key):
                policy = copy.deepcopy(self.p)
                policy["experiments"][section][key] = value
                with self.assertRaises(ValueError):
                    e.simulate("600000", bars([10] * 20), "combined", policy)

    def test_missing_experiment_settings_rejected(self):
        del self.p["experiments"]["time"]
        with self.assertRaises(ValueError):
            self.run_strategy([10] * 20, "hold")

    def test_simulate_also_rejects_bad_ohlc(self):
        bs = bars([10] * 10)
        bs[5]["low"] = 11
        with self.assertRaises(ValueError):
            e.simulate("600000", bs, "hold", self.p)

    def test_cli_accepts_numeric_codes(self):
        import core_value as cv
        expected = {s: {"available": False} for s in e.STRATEGIES}
        with patch.object(pr, "load_policy", return_value=self.p), \
             patch.object(cv, "load_core", return_value={"sleeves": {}}), \
             patch("builtins.open", mock_open(read_data=json.dumps(bars([10] * 20)))), \
             patch.object(e, "compare", return_value=expected), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(e.main(["--codes", "600000", "--asof", "2026-11-12"]), 0)

    def test_cli_rejects_path_like_codes(self):
        import core_value as cv
        with patch.object(pr, "load_policy", return_value=self.p), \
             patch.object(cv, "load_core", return_value={"sleeves": {}}), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                e.main(["--codes", "x/600000", "--asof", "2026-11-12"])
            self.assertEqual(exc.exception.code, 2)

    def test_cash_never_negative_and_qty_integer(self):
        r = self.run_strategy([10, 10, 10, 9, 8, 7, 12, 15, 20, 21, 22], "combined")
        self.assertTrue(all(x["cash"] >= -1e-6 for x in r["history"]))
        self.assertTrue(all(isinstance(t["qty"], int) for t in r["trades"]))


if __name__ == "__main__":
    unittest.main()
