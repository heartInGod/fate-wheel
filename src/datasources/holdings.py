"""持仓数据：读取 holdings.csv / account.csv，并与实时行情合并。

holdings.csv 的「可用数量」是某一天的快照，跨过交易日后会失真（T+1 解冻）。
这里用 datasources.ops 按交易日历把它推进到今天，details 见 ops 模块文档。
"""
import csv
import datetime
import os

from ._http import normalize, to_float

from paths import ROOT
from datamul import data_path
HOLDINGS_CSV = data_path("holdings.csv")
ACCOUNT_CSV = data_path("account.csv")


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _parse_base_date(s):
    s = str(s or "").strip().replace("/", "-")
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def holdings(derive=True):
    """持仓清单（holdings.csv），数值列已转为数字，附带规范化代码。

    derive=True 时把「可用数量」按 T+1 规则推进到今天（见 datasources.ops）。
    """
    rows = []
    for r in _read_csv(HOLDINGS_CSV):
        if not (r.get("代码") or "").strip():
            continue
        n = normalize(r["代码"])
        rows.append({"code": n["code"], "market": n["market"], "name": r.get("名称"),
                     "type": r.get("市场"), "shares": to_float(r.get("持仓数量")),
                     "available": to_float(r.get("可用数量")), "cost": to_float(r.get("成本价")),
                     "currency": r.get("币种"), "stance": r.get("当前定位"),
                     "base_date": _parse_base_date(r.get("可用基准日"))})
    if not derive:
        return rows
    try:
        from . import ops
        days = ops.trading_days(count=60)
        return ops.derive_available(rows, ops.load_operations(), days)
    except Exception:  # noqa: BLE001
        return rows


def account():
    """账户概况（account.csv 最后一行）。"""
    rows = _read_csv(ACCOUNT_CSV)
    if not rows:
        return {}
    r = rows[-1]
    return {"updated": r.get("更新日期"), "total_assets": to_float(r.get("总资产")),
            "market_value": to_float(r.get("总市值")), "cash_available": to_float(r.get("可用资金")),
            "float_pnl": to_float(r.get("浮动盈亏")), "position_ratio": r.get("仓位")}


def merge_position(quote, h):
    """把持仓字段并入行情：数量、可用、成本、定位、市值、盈亏。"""
    q = dict(quote)
    q.update({"position_name": h.get("name"), "shares": h.get("shares"), "available": h.get("available"),
              "cost": h.get("cost"), "currency": h.get("currency"), "stance": h.get("stance"),
              "market_value": None, "pnl": None, "pnl_pct": None})
    # T+1 推导的附加信息（locked_today / available_derived / base_date 等）
    for k in ("locked_today", "available_derived", "base_available", "base_date"):
        if k in h:
            q[k] = h[k]
    p, s, c = q.get("price"), h.get("shares"), h.get("cost")
    if p is not None and s and c:
        q["market_value"] = round(s * p, 2)
        q["pnl"] = round(s * (p - c), 2)
        q["pnl_pct"] = round((p / c - 1) * 100, 2)
    return q
