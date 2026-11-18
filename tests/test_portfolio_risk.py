#!/usr/bin/env python3
import os as _os, sys as _sys
_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _root)
from fw.paths import setup_sys_path as _ssp; _ssp()
import copy
import json
import unittest
from unittest.mock import patch, mock_open
import portfolio_risk as p


class PortfolioRiskTests(unittest.TestCase):
    def setUp(self):
        self.policy = p.load_policy()
        self.snap = {"market": {"now": "2026-11-12 10:00"}, "account": {"total_assets": 100000, "cash_available": 50000},
                     "positions": [{"code": "A", "price": 10, "shares": 2000, "market_value": 20000},
                                   {"code": "B", "price": 10, "shares": 3000, "market_value": 30000}]}
        self.core = {"sleeves": {"A": {"sleeve": "core"}, "B": {"sleeve": "trade"}},
                     "stocks": {"A": {"target": 10, "industry": "半导体"}, "B": {"target": 10, "industry": "半导体"}}}

    def assess(self):
        return p.assess_portfolio(self.snap, {}, self.core, self.policy, {"available": False})

    def test_scenario(self):
        r = p.scenarios({"target": 100, "price": 80}, self.policy)
        self.assertAlmostEqual(r["cases"]["bear"]["target"], 64)
        self.assertAlmostEqual(r["downside_frac"], 0.2)
        self.assertTrue(r["assumption"])

    def test_target_upside_does_not_imply_no_risk(self):
        self.core["stocks"]["A"]["target"] = 100
        r = self.assess()["rows"]["A"]
        self.assertEqual(r["valuation_downside_frac"], 0)
        self.assertGreaterEqual(r["downside_frac"], 0.3)

    def test_cross_sleeve_exposure_counted_once(self):
        r = self.assess()
        self.assertEqual(r["groups"]["industry:半导体"]["value"], 50000)
        self.assertEqual(r["groups"]["factor:电子产业链"]["value"], 50000)
        self.assertAlmostEqual(r["known_loss_yuan"], 18000)
        self.assertFalse(r["enforced"])

    def test_unknown_risk_not_zero(self):
        del self.core["stocks"]["B"]["target"]
        r = self.assess()
        self.assertIsNone(r["rows"]["B"]["downside_frac"])
        self.assertEqual(r["unknown_valuation_value"], 30000)
        self.assertEqual(p.buy_capacity("A", r, 10)["amount"], 0)

    def test_stale_valuation_not_usable(self):
        self.core["_health"] = {"status": "stale"}
        r = self.assess()
        self.assertEqual(r["unknown_valuation_value"], 50000)

    def test_foreign_market_value_not_guessed(self):
        self.snap["positions"][0].update(currency="HKD", market="hk", market_value=None)
        r = self.assess()
        self.assertTrue(r["market_value_missing"])
        self.assertIsNone(r["valuation_coverage_frac"])

    def test_top10_unknown_remainder_and_duplicate_leaf(self):
        self.snap["positions"][0]["is_etf"] = True
        self.snap["positions"][1]["is_etf"] = True
        with patch.object(p, "etf_components", return_value={"weights": {"600000": 0.2}, "unknown_frac": 0.8, "source": "fixture"}):
            r = self.assess()
        self.assertEqual(r["groups"]["underlying:600000"]["value"], 10000)
        self.assertEqual(r["groups"]["underlying:未知ETF余项"]["value"], 40000)

    def test_nav_corrects_deposit_and_withdrawal(self):
        obs = [{"date": "2026-11-10", "assets": 100, "external_flow": 0},
               {"date": "2026-11-11", "assets": 120, "external_flow": 20},
               {"date": "2026-11-12", "assets": 90, "external_flow": -10}]
        r = p.nav_report(obs)
        self.assertTrue(r["available"])
        self.assertAlmostEqual(r["unit_nav"], 100 / 120)
        self.assertAlmostEqual(r["drawdown_frac"], 1 - 100 / 120)

    def test_missing_flow_never_assumed_zero(self):
        self.assertFalse(p.nav_report([{"date": "2026-11-10", "assets": 100}, {"date": "2026-11-11", "assets": 110}])["available"])

    def test_nav_duplicate_rejected(self):
        r = p.nav_report([{"date": "2026-11-10", "assets": 100, "external_flow": 0}] * 2)
        self.assertFalse(r["available"])

    def test_hysteresis_and_recovery(self):
        n = {"available": True, "drawdown_frac": 0.06}
        self.assertEqual(p.drawdown_state(n, self.policy, "reduce")["state"], "reduce")
        n["drawdown_frac"] = 0.03
        self.assertEqual(p.drawdown_state(n, self.policy, "reduce")["state"], "normal")

    def test_snapshot_drawdown_replays_hysteresis_history(self):
        nav = {"available": True, "drawdown_frac": 0.06,
               "history": [{"drawdown_frac": 0}, {"drawdown_frac": 0.13}, {"drawdown_frac": 0.06}]}
        self.assertEqual(p.drawdown_state(nav, self.policy)["state"], "reduce")
        nav["drawdown_frac"] = 0.03
        nav["history"].append({"drawdown_frac": 0.03})
        self.assertEqual(p.drawdown_state(nav, self.policy)["state"], "normal")

    def test_policy_rejects_incomplete_invalid_or_active(self):
        cases = []
        incomplete = copy.deepcopy(self.policy)
        del incomplete["limits"]["single_loss_frac"]
        cases.append(incomplete)
        wrong = copy.deepcopy(self.policy)
        wrong["drawdown"]["watch_frac"] = 0.2
        cases.append(wrong)
        active = copy.deepcopy(self.policy)
        active["mode"] = "active"
        cases.append(active)
        wrong_type = copy.deepcopy(self.policy)
        wrong_type["scenarios"]["bear"]["eps_factor"] = "0.8"
        cases.append(wrong_type)
        for cfg in cases:
            with patch("builtins.open", mock_open(read_data=json.dumps(cfg))):
                loaded = p.load_policy()
            self.assertTrue(loaded.get("errors"))
            self.assertEqual(loaded["mode"], "shadow")

    def test_sequential_budget_reservation(self):
        r = self.assess()
        before = r["cash"]
        p.reserve_buy("A", 100, 10, r)
        self.assertEqual(r["cash"], before - 1000)
        self.assertEqual(r["groups"]["industry:半导体"]["value"], 51000)


if __name__ == "__main__":
    unittest.main()
