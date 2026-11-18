#!/usr/bin/env python3
import os as _os, sys as _sys
_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _root)
from fw.paths import setup_sys_path as _ssp; _ssp()
"""长线风控集成回归；全部模拟输入，不联网、不修改业务数据。"""
import copy
import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import core_value as cv
import signal_store as st
import signals_engine as se


class RiskIntegration(unittest.TestCase):
    def setUp(self):
        self.params = st.load_params()
        self.core = {"asof": "20261112", "params": dict(cv.DEFAULTS),
                     "sleeves": {"600000": {"sleeve": "core"}},
                     "stocks": {"600000": {"code": "600000", "name": "测试股", "sleeve": "core",
                                            "held": True, "shares": 1000, "available": 1000,
                                            "value": 10000, "price": 10, "cost": 8, "target": 12,
                                            "zone": "buy", "action": "旧买入", "qty": 900, "review": []}}}
        self.snap = {"mode": "intraday", "market": {"now": "2026-11-12 10:20", "a_open_today": True},
                     "account": {"total_assets": 100000, "cash_available": 20000},
                     "positions": [{"code": "600000", "name": "测试股", "shares": 1000,
                                    "available": 1000, "price": 11, "cost": 8,
                                    "market_value": 11000, "is_today": True,
                                    "low": 10.8, "high": 11.1, "prev_close": 11, "tech": {}}]}

    def status(self, core=None, snap=None, risk=None):
        normal = {"state": "normal", "block_add": False, "reasons": []}
        real_assess = cv.rc.assess
        def assessed(code, row, *args, **kwargs):
            return real_assess(code, row, *args, **kwargs) if row.get("risk_override") else risk or normal
        with patch.object(cv, "load_core", return_value=copy.deepcopy(core or self.core)), \
             patch.object(cv.rc, "load_thesis", return_value={"items": {}}), \
             patch.object(cv.rc, "load_ledger", return_value={}), \
             patch.object(cv.rc, "assess", side_effect=assessed):
            return cv.status(self.params, snap or self.snap, "20261112")

    def test_hold_does_not_reuse_buy(self):
        row = self.status()["items"][0]
        self.assertEqual(row["zone"], "hold")
        self.assertEqual(row["review_qty"], 0)
        self.assertNotEqual(row["review_action"], "旧买入")

    def test_live_available_zero_never_falls_back(self):
        self.snap["positions"][0].update(price=13, available=0)
        self.assertEqual(self.status()["items"][0]["review_qty"], 0)

    def test_cash_zero_never_falls_back(self):
        self.snap["positions"][0].update(price=8, market_value=8000)
        self.snap["account"]["cash_available"] = 0
        self.assertEqual(self.status()["items"][0]["review_qty"], 0)

    def test_removed_holding_not_recommended(self):
        self.snap["positions"] = []
        self.assertEqual(self.status()["items"], [])

    def test_stale_target_never_generates_sell(self):
        self.core["_health"] = {"status": "stale", "block_add": True, "note": "估值过期"}
        self.snap["positions"][0].update(price=15)
        result = self.status()
        self.assertTrue(result["safe_mode"])
        self.assertEqual(result["items"][0]["qty"], 0)
        self.assertEqual(result["sleeves"]["600000"], "core")

    def test_confirmed_exit_overrides_cost_and_stale(self):
        self.core["_health"] = {"status": "stale", "block_add": True}
        self.snap["positions"][0].update(price=4, available=300, cost=20)
        risk = {"state": "exit", "block_add": True, "desired_sell_qty": 1000, "reasons": ["关键前提失效"]}
        row = self.status(risk=risk)["items"][0]
        self.assertEqual(row["qty"], 300)
        self.assertIsNone(row["cost_block"])
        self.assertIn("风险退出", row["review_action"])

    def test_missing_market_price_does_not_reuse_old(self):
        self.snap["positions"][0]["price"] = None
        row = self.status()["items"][0]
        self.assertIsNone(row["price"])
        self.assertEqual(row["qty"], 0)

    def test_financial_revision_accumulates_same_year(self):
        old = {"eps": 10, "eps_year": 2027}
        anchor, cut = cv.eps_revision(old, {"eps": 9, "eps_year": 2027})
        self.assertEqual(cut, 10)
        anchor, cut = cv.eps_revision({"eps": 9, "eps_year": 2027, "eps_reference": anchor},
                                      {"eps": 8.1, "eps_year": 2027})
        self.assertEqual(cut, 19)
        self.assertEqual(cv.eps_revision(old, {"eps": 5, "eps_year": 2028}), (5, 0))

    def test_loader_preserves_stale_and_registry(self):
        root = os.path.join(cv.ROOT, "backups", "risk_validation")
        os.makedirs(root, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as tmp, patch.object(cv, "ROOT", tmp):
            path = os.path.join(tmp, "core_auto.json")
            with open(path, "w") as f:
                json.dump(self.core, f)
            loaded = cv.load_core(self.params, "20261201")
            self.assertEqual(loaded["_health"]["status"], "stale")
            self.assertEqual(cv.sleeve_of("600000", self.params, loaded), "core")
            cv.remember_sleeves(loaded)
            # 损坏估值而不是删除文件；测试归属备份回退。
            with open(path, "w") as f:
                f.write("broken")
            loaded = cv.load_core(self.params, "20261201")
            self.assertEqual(loaded["_health"]["status"], "missing")
            self.assertEqual(cv.sleeve_of("600000", self.params, loaded), "core")

    def test_missing_stance_cannot_override_force_trade(self):
        missing = {"stocks": {}, "sleeves": {}, "_health": {"status": "missing", "block_add": True}}
        self.snap["positions"][0]["stance"] = "长线仓（持有区）"
        self.params["sleeves"]["trade"]["force"] = ["600000"]
        self.assertEqual(self.status(core=missing)["sleeves"]["600000"], "trade")
        self.assertEqual(se.sleeves_of(self.snap, self.params, missing)["600000"], "trade")

    def test_current_trade_force_overrides_old_core_risk(self):
        self.params["sleeves"]["trade"]["force"] = ["600000"]
        risk = {"state": "exit", "block_add": True, "desired_sell_qty": 1000, "reasons": ["旧长线风险"]}
        result = self.status(risk=risk)
        self.assertEqual(result["sleeves"]["600000"], "trade")
        self.assertEqual(result["items"], [])

    def test_risk_exit_not_suppressed_by_t_or_ev(self):
        self.params["auto"]["enabled"] = False
        self.snap["positions"][0].update(price=11, high=11.01, low=10, available=500)
        risk = {"state": "exit", "block_add": True, "desired_sell_qty": 1000,
                "cycle_id": "test-risk", "reasons": ["逻辑失效"]}
        result = self.status(risk=risk)
        with patch.object(cv, "status", return_value=result), patch.object(cv, "load_core", return_value=self.core), \
             patch.object(st, "load_signals", return_value={}):
            output = se.run(self.snap, self.params, record=False)
        exits = [c for c in output["candidates"] if c.get("risk_exit")]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["qty"], 500)
        self.assertFalse(any(c["type"] in ("t_high", "t_low") for c in output["candidates"]))


if __name__ == "__main__":
    unittest.main()
