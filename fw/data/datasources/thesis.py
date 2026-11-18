"""持仓定位前提（thesis.json）：把「当前定位」背后的基本面判断拆出来，带日期、依据和状态，按期复核。

holdings.csv 的「当前定位」只写操作纪律（如「只做T不加仓」），由 09:00 盘前按模型分析自动维护（propose + apply，张小凡可 revert）；
定位依赖的基本面判断（如「钨价下行」「Q3 环比 -22%」）写在 thesis.json 的 premises 里：

  {"stale_days": {"商品价格": 7, "行业": 14, "公司": 30, "宏观": 14, "default": 14},
   "items": {"000657": {"name": "中钨高新",
                        "premises": [{"id": "P1", "text": "钨价下行", "kind": "商品价格",
                                      "supports": "只做T不加仓", "query": "APT 钨精矿 价格",
                                      "status": "待核实|成立|存疑|失效", "since": "2026-10-30",
                                      "checked": "2026-11-10", "evidence": "…", "source": "…"}],
                        "proposal": {"stance": "新定位", "reason": "…", "date": "…", "status": "pending"},
                        "commodities": ["W_ORE", "APT"],   # 关联价格品种（datasources/commodity.py SERIES）
                        "history": [...]}}}

本模块只读（不联网），写入由根目录 thesis.py 完成。
"""
import datetime
import json
import os

from ._http import normalize

from fw.paths import ROOT
from fw.datamul import data_path
THESIS_JSON = data_path("thesis.json")

STATUSES = ("待核实", "成立", "存疑", "失效")
DEFAULT_STALE = {"商品价格": 7, "行业": 14, "公司": 30, "宏观": 14, "default": 14}


def norm(code):
    try:
        return normalize(code)["code"]
    except Exception:  # noqa: BLE001
        return str(code).strip()


def load(path=None):
    path = path or THESIS_JSON
    if not os.path.exists(path):
        return {"stale_days": dict(DEFAULT_STALE), "items": {}}
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("stale_days", dict(DEFAULT_STALE))
    d.setdefault("items", {})
    return d


def _date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def premise_view(p, stale_days, today=None):
    """单条前提 + age_days（距上次核实天数）+ stale（需要今天核实）。"""
    today = today or datetime.date.today()
    ttl = p.get("ttl_days") or stale_days.get(p.get("kind") or "", stale_days.get("default", 14))
    last = _date(p.get("checked")) or None
    age = (today - last).days if last else None
    stale = p.get("status") in (None, "", "待核实") or age is None or age > ttl
    v = {k: p.get(k) for k in ("id", "text", "kind", "supports", "query", "status", "since", "checked",
                              "evidence", "source")}
    v.update({"age_days": age, "ttl_days": ttl, "stale": stale})
    return v


def summary(code, data=None, today=None):
    """某只持仓的前提摘要，供 snapshot 并入 positions[].thesis。"""
    data = data if data is not None else load()
    it = data["items"].get(norm(code))
    if not it:
        return {"premises": [], "stale": [], "invalid": [], "missing": True, "proposal": None, "commodities": [],
                "changed": None, "reverted": None}
    ps = [premise_view(p, data["stale_days"], today) for p in it.get("premises") or []
          if p.get("status") != "已归档"]
    prop = it.get("proposal")
    today_d = _date(today) if isinstance(today, str) else (today or datetime.date.today())
    last = next((h for h in reversed(it.get("history") or []) if h.get("op") in ("apply", "revert")), None)
    changed = None
    if last and last.get("op") == "apply":
        d = _date((last.get("at") or "")[:10])
        if d and (today_d - d).days <= 3:
            changed = {"date": str(d), "from": last.get("from"), "stance": last.get("stance"),
                       "by": last.get("by") or "张小凡", "reason": last.get("reason")}
    rv = next((h for h in reversed(it.get("history") or []) if h.get("op") == "revert"), None)
    reverted = None
    if rv:
        d = _date((rv.get("at") or "")[:10])
        if d and (today_d - d).days <= 14:
            reverted = {"date": str(d), "undone": rv.get("from"), "restored": rv.get("stance"),
                        "reason": rv.get("reason")}
    return {"changed": changed, "reverted": reverted, "risk_contract": it.get("risk_contract"),
            "premises": ps,
            "stale": [p["id"] for p in ps if p["stale"]],
            "invalid": [p["id"] for p in ps if p["status"] in ("失效", "存疑")],
            "missing": not ps,
            "proposal": prop if prop and prop.get("status") == "pending" else None,
            "commodities": it.get("commodities") or []}


def summaries(codes, today=None):
    data = load()
    return {norm(c): summary(c, data, today) for c in codes}
