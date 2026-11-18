#!/usr/bin/env python3
import os as _os, sys as _sys
_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _root)
_sys.path.insert(0, _os.path.join(_root, "common"))
from paths import setup_sys_path as _ssp; _ssp()
import copy
import os
import tempfile
import unittest

import risk_core as r


class CoreRiskTests(unittest.TestCase):
    def setUp(self):
        self.row = {"shares": 1000, "available": 1000, "price": 10, "review": []}
        self.data = {"items": {"600000": {"premises": [{"id": "P1", "kind": "公司", "status": "失效",
                                                        "checked": "2026-11-12", "evidence": "确认数据", "source": "公告"}],
                                           "risk_contract": {"thesis": "测试逻辑", "metrics": ["利润"],
                                                             "due_date": "2027-12-31", "recovery": "新证据确认恢复",
                                                             "critical_premises": ["P1"], "failure_action": "reduce",
                                                             "reduce_fraction": 0.5}}}}
        root = os.path.join(r.ROOT, "backups", "risk_validation")
        os.makedirs(root, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "ledger.jsonl")

    def assess(self, row=None, data=None):
        return r.assess("600000", row or self.row, data or self.data, "20261112", r.load_ledger(self.path))

    def test_critical_failure(self):
        p = self.assess()
        self.assertEqual((p["state"], p["desired_sell_qty"], p["target_remaining_shares"]), ("reduce", 500, 500))

    def test_unknown_fraction_never_guessed(self):
        del self.data["items"]["600000"]["risk_contract"]["reduce_fraction"]
        self.assertEqual(self.assess()["state"], "pause_add")

    def test_stale_failure_never_sells(self):
        self.data["items"]["600000"]["premises"][0]["checked"] = "2026-01-01"
        self.assertEqual(self.assess()["desired_sell_qty"], 0)

    def test_future_evidence_never_sells(self):
        self.data["items"]["600000"]["premises"][0]["checked"] = "2027-01-01"
        self.assertEqual(self.assess()["desired_sell_qty"], 0)

    def test_noncritical_failure_observes(self):
        self.data["items"]["600000"]["risk_contract"]["critical_premises"] = []
        self.assertEqual(self.assess()["state"], "observe")

    def test_missing_contract_pauses(self):
        del self.data["items"]["600000"]["risk_contract"]
        self.assertEqual(self.assess()["state"], "pause_add")

    def test_missing_due_date_pauses_add(self):
        del self.data["items"]["600000"]["risk_contract"]["due_date"]
        plan = self.assess()
        self.assertEqual(plan["state"], "pause_add")
        self.assertFalse(plan["coverage"]["complete"])

    def test_sent_is_not_fill(self):
        p = self.assess()
        r.register_plan(p, self.row, self.path)
        r.mark(p["cycle_id"], "sent", path=self.path)
        self.assertEqual(self.assess()["desired_sell_qty"], 500)

    def test_partial_fill_and_idempotence(self):
        p = self.assess()
        r.register_plan(p, self.row, self.path)
        r.mark(p["cycle_id"], "filled", 200, "deal1", self.path)
        r.mark(p["cycle_id"], "filled", 200, "deal1", self.path)
        self.assertEqual(self.assess()["desired_sell_qty"], 300)
        r.mark(p["cycle_id"], "filled", 300, "deal2", self.path)
        row = dict(self.row, shares=500, available=500)
        result = self.assess(row)
        self.assertEqual(result["desired_sell_qty"], 0)
        self.assertEqual(result["target_remaining_shares"], 500)

    def test_overfill_rejected(self):
        p = self.assess()
        r.register_plan(p, self.row, self.path)
        with self.assertRaises(ValueError):
            r.mark(p["cycle_id"], "filled", 501, "deal1", self.path)

    def test_registered_failure_survives_stale(self):
        p = self.assess()
        r.register_plan(p, self.row, self.path)
        self.data["items"]["600000"]["premises"][0]["checked"] = "2026-01-01"
        self.assertEqual(self.assess()["desired_sell_qty"], 500)

    def test_no_available_keeps_risk(self):
        p = self.assess(dict(self.row, available=0))
        self.assertEqual(p["desired_sell_qty"], 0)
        self.assertEqual(p["execution"]["remaining_qty"], 500)
        self.assertTrue(p["execution"]["blocked"])

    def test_resolve_requires_evidence(self):
        p = self.assess()
        r.register_plan(p, self.row, self.path)
        with self.assertRaises(ValueError):
            r.mark(p["cycle_id"], "closed", path=self.path)
        r.mark(p["cycle_id"], "closed", path=self.path, reason="新证据恢复", source="新公告")
        self.assertEqual(self.assess()["state"], "pause_add")

    def test_unresolved_logic_not_overridden_by_valuation(self):
        plan = self.assess()
        r.register_plan(plan, self.row, self.path)
        self.data["items"]["600000"]["premises"][0]["checked"] = "2026-01-01"
        row = dict(self.row, risk_override={"state": "exit", "trigger": "valuation:all", "reduce_fraction": 1})
        result = self.assess(row)
        self.assertEqual(result["risk_source"], "logic")
        self.assertEqual(result["cycle_id"], plan["cycle_id"])

    def test_closed_cycle_cannot_reopen_by_sending(self):
        plan = self.assess()
        r.register_plan(plan, self.row, self.path)
        r.mark(plan["cycle_id"], "closed", path=self.path, reason="恢复", source="新公告")
        for status in ("sent", "blocked", "filled"):
            with self.assertRaises(ValueError):
                r.mark(plan["cycle_id"], status, 100, "fill", self.path)
        r.mark(plan["cycle_id"], "reopened", path=self.path, reason="重新失效", source="公告")
        self.assertEqual(self.assess()["state"], "reduce")

    def test_valuation_cycle_not_hard_logic(self):
        row = dict(self.row, risk_override={"state": "reduce", "trigger": "valuation_half:12", "reduce_fraction": 0.5})
        self.data["items"]["600000"]["premises"][0]["status"] = "成立"
        p = self.assess(row)
        r.register_plan(p, row, self.path)
        self.assertEqual(p["risk_source"], "valuation")
        self.assertEqual(self.assess()["state"], "normal")


if __name__ == "__main__":
    unittest.main()
