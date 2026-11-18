#!/usr/bin/env python3
"""B：估值情景、跨仓暴露/风险预算、资金流校正净值。默认 shadow，只分析不交易。

python3 main.py portfolio --snapshot cache/snapshot_premarket.json [--out reports/risk.json]
python3 main.py portfolio nav --date YYYY-MM-DD --assets N --external-flow N --source 券商证据
新阈值来自 risk_policy.json（均为试验假设）。ETF只穿透已知前十大，剩余明确未知。
净值需显式逐日资产与外部资金流；不会把重复账户缓存当真实历史净值。
"""
import argparse
import csv
import datetime as dt
import fcntl
import json
import math
import os
import re

from paths import ROOT
from datamul import data_path
POLICY_PATH = data_path("risk_policy.json")
NAV_PATH = os.path.join(ROOT, "logs", "core_risk", "nav.jsonl")


def number(x):
    try:
        y = float(x)
        return y if math.isfinite(y) else None
    except (ValueError, TypeError):
        return None


def load_policy(path=None):
    try:
        with open(path or POLICY_PATH, encoding="utf-8") as f:
            p = json.load(f)
        if not isinstance(p, dict) or p.get("mode") not in ("off", "shadow", "active"):
            raise ValueError("模式错误")
        if p["mode"] == "active":
            raise ValueError("active尚未开放：当前只交付影子分析，不能绕过全面集成和启用验收")
        for name in ("bear", "base", "bull"):
            s = (p.get("scenarios") or {}).get(name)
            if not isinstance(s, dict) or not all(isinstance(s.get(k), (int, float)) and not isinstance(s[k], bool)
                    and number(s[k]) is not None and 0 < s[k] <= 3 for k in ("eps_factor", "pe_factor")):
                raise ValueError("情景倍数必须完整、为正且有限数值")
        required = {"limits": ("single_loss_frac", "portfolio_loss_frac", "group_weight_frac", "cash_floor_frac"),
                    "stress": ("market_down_frac", "group_down_frac"),
                    "drawdown": ("watch_frac", "reduce_frac", "exit_frac", "recovery_frac")}
        for section, keys in required.items():
            cfg = p.get(section)
            if not isinstance(cfg, dict) or any(not isinstance(cfg.get(k), (int, float)) or isinstance(cfg[k], bool)
                    or number(cfg[k]) is None or not 0 <= cfg[k] <= 1 for k in keys):
                raise ValueError("风险比例必须完整并为0~1之间的有限数值")
        d = p["drawdown"]
        if not d["recovery_frac"] < d["watch_frac"] < d["reduce_frac"] < d["exit_frac"]:
            raise ValueError("回撤阈值须满足 recovery < watch < reduce < exit")
        return p
    except (OSError, ValueError, TypeError) as exc:
        return {"mode": "shadow", "errors": ["风险参数不可用：" + str(exc)]}


def scenarios(row, policy, price=None):
    price = number(price if price is not None else row.get("price"))
    target = number(row.get("target"))
    if price is None or price <= 0 or target is None or target <= 0:
        return {"available": False, "reason": "缺现价/有效目标价，不把未知下行当0", "cases": {}}
    output = {}
    for name, s in (policy.get("scenarios") or {}).items():
        # 对EPS×PE和ETF周期盈利×PE同时做倍数压力；以已封顶基准为锚，不假装精确预测。
        value = target * s["eps_factor"] * s["pe_factor"]
        output[name] = {"target": round(value, 4), "return_frac": value / price - 1,
                        "eps_factor": s["eps_factor"], "pe_factor": s["pe_factor"]}
    bear = output.get("bear")
    return {"available": bool(bear), "cases": output, "downside_frac": max(0, -bear["return_frac"]) if bear else None,
            "assumption": True, "method": "已封顶基准目标×盈利变化×估值倍数变化；不是预测或概率",
            "model_kind": "index_earnings_pe" if row.get("kind") == "etf" else "eps_pe"}


def etf_components(code):
    path = os.path.join(ROOT, "profiles", str(code) + ".md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {"weights": {}, "unknown_frac": 1.0, "source": path, "report_date": None}
    weights = {}
    table = text.split("## 前十大持仓", 1)[-1] if "## 前十大持仓" in text else ""
    for line in table.splitlines():
        cells = [s.strip() for s in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and re.fullmatch(r"[A-Z][A-Z0-9.\-]*|\d{6}", cells[0]):
            pct = number(cells[2])
            if pct is not None and 0 <= pct <= 100:
                weights[cells[0]] = pct / 100
    if sum(weights.values()) > 1.00001:
        return {"weights": {}, "unknown_frac": 1.0, "source": path, "error": "ETF明细权重异常", "report_date": None}
    return {"weights": weights, "known_frac": sum(weights.values()), "unknown_frac": max(0, 1 - sum(weights.values())),
            "source": path, "report_date": None, "quality": "top10_only_report_date_unknown"}


def nav_report(observations):
    if len(observations) < 2:
        return {"available": False, "reason": "至少需要2个明确资产/外部资金流观测；缓存不代替真实净值", "observations": len(observations)}
    values, nav, peak, max_dd, prev, last_date = [], 1.0, 1.0, 0.0, None, None
    for o in observations:
        try:
            date = dt.date.fromisoformat(o["date"])
        except (ValueError, KeyError, TypeError):
            return {"available": False, "reason": "净值日期无效"}
        assets = number(o.get("assets"))
        flow = number(o.get("external_flow"))
        if assets is None or assets <= 0 or flow is None or last_date and date <= last_date:
            return {"available": False, "reason": "资产/资金流缺失或日期重复乱序，不能可靠计算回撤"}
        if prev is not None:
            factor = (assets - flow) / prev
            if factor <= 0:
                return {"available": False, "reason": "资产与资金流不一致"}
            nav *= factor
        peak = max(peak, nav)
        dd = max(0, 1 - nav / peak)
        max_dd = max(max_dd, dd)
        values.append({"date": o["date"], "unit_nav": nav, "drawdown_frac": dd})
        prev, last_date = assets, date
    return {"available": True, "unit_nav": nav, "total_return_frac": nav - 1, "drawdown_frac": values[-1]["drawdown_frac"],
            "max_drawdown_frac": max_dd, "history": values, "method": "链式收益=(期末资产−期末外部净入金)/上期资产；资金流按期末处理"}


def drawdown_state(nav, policy, previous=None):
    if not nav.get("available"):
        return {"state": "unknown", "reason": nav.get("reason"), "block_add": False, "enforced": False}
    if previous is None:
        previous = "normal"
        for point in nav.get("history") or []:
            previous = drawdown_state({"available": True, "drawdown_frac": point["drawdown_frac"]}, policy, previous)["state"]
    cfg = policy.get("drawdown") or {}
    dd = nav["drawdown_frac"]
    state = "exit" if dd >= cfg.get("exit_frac", 1) else "reduce" if dd >= cfg.get("reduce_frac", 1) else "watch" if dd >= cfg.get("watch_frac", 1) else "normal"
    ranks = {"normal": 0, "watch": 1, "reduce": 2, "exit": 3}
    if previous in ranks and ranks[previous] > ranks[state] and dd > cfg.get("recovery_frac", 0):
        state = previous
    return {"state": state, "block_add": state != "normal", "drawdown_frac": dd,
            "enforced": policy.get("mode") == "active", "recovery_requires": "回撤恢复至恢复线，并复查逻辑与数据有效性"}


def load_nav(path=None):
    try:
        with open(path or NAV_PATH, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return [{"error": "净值台账不可用"}]


def record_nav(date, assets, external_flow, source, path=None):
    dt.date.fromisoformat(date)
    if not source or number(assets) is None or assets <= 0 or number(external_flow) is None:
        raise ValueError("净值观测需正资产、明确外部资金流和来源")
    path = path or NAV_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        rows = [json.loads(line) for line in f if line.strip()]
        if rows and date <= rows[-1]["date"]:
            raise ValueError("净值观测只追加，不覆盖或重复日期")
        f.write(json.dumps({"date": date, "assets": assets, "external_flow": external_flow, "source": source}, ensure_ascii=False) + "\n")
    return nav_report(rows + [{"date": date, "assets": assets, "external_flow": external_flow}])


def assess_portfolio(snap, params, core, policy=None, nav=None):
    p = load_policy() if policy is None else policy
    acc = snap.get("account") or {}
    assets, cash = number(acc.get("total_assets")), number(acc.get("cash_available"))
    cfg = p.get("limits") or {}
    stocks = (core or {}).get("stocks") or {}
    groups, rows, alerts = {}, {}, list(p.get("errors") or [])
    known_loss, unknown_value, gross = 0.0, 0.0, 0.0
    def add_group(dimension, label, code, value, quality, source):
        key = dimension + ":" + label
        e = groups.setdefault(key, {"dimension": dimension, "label": label, "value": 0.0, "codes": [], "quality": [], "sources": []})
        e["value"] += value
        if code not in e["codes"]:
            e["codes"].append(code)
        if quality not in e["quality"]:
            e["quality"].append(quality)
        if source not in e["sources"]:
            e["sources"].append(source)
    for pos in snap.get("positions") or []:
        code = str(pos.get("code"))
        shares, price = number(pos.get("shares")), number(pos.get("price"))
        if not shares or shares <= 0:
            continue
        v = number(pos.get("market_value"))
        foreign = str(pos.get("currency", "CNY")).upper() not in ("CNY", "RMB", "人民币") or pos.get("market") == "hk"
        if v is None and not foreign and price:
            v = shares * price
        if v is None:
            alerts.append(f"{code} 本币市值缺失，不用港币价格直接当人民币")
            rows[code] = {"value": None, "downside_frac": None, "risk_contribution_frac": None}
            continue
        gross += v
        r = stocks.get(code) or {}
        scenario = scenarios(r, p, price)
        if (core or {}).get("_health", {}).get("status") not in (None, "fresh"):
            scenario["stale_valuation"] = True
            scenario["available"] = False
        valuation_down = scenario.get("downside_frac") if scenario.get("available") else None
        risk_down = max(valuation_down, (p.get("stress") or {}).get("market_down_frac", 0)) if valuation_down is not None else None
        loss = v * risk_down if risk_down is not None else None
        if loss is None:
            unknown_value += v
        else:
            known_loss += loss
        e = rows[code] = {"value": v, "price": price, "sleeve": ((core or {}).get("sleeves", {}).get(code) or {}).get("sleeve", "trade"),
                          "weight_frac": v / assets if assets and assets > 0 else None, "scenario": scenario,
                          "downside_frac": risk_down, "valuation_downside_frac": valuation_down,
                          "risk_method": "max(估值情景下行,全市场压力假设)，目标价高不意味着风险为0",
                          "risk_loss_yuan": loss, "risk_contribution_frac": loss / assets if loss is not None and assets and assets > 0 else None}
        if e["risk_contribution_frac"] is not None and e["risk_contribution_frac"] > cfg.get("single_loss_frac", 1):
            alerts.append(f"{code} 假设下行损失占总资产超过单股风险预算")
        proxy = (p.get("etf_proxies") or {}).get(code) or {}
        industry = proxy.get("industry") if pos.get("is_etf") else r.get("industry") or pos.get("industry")
        quality = proxy.get("quality", "company_industry")
        source = proxy.get("source", "core_auto.json:industry")
        if industry:
            add_group("industry", industry, code, v, quality, source)
        else:
            add_group("industry", "未知", code, v, "unknown", "missing")
        for factor, terms in (p.get("factor_rules") or {}).items():
            if industry and any(t in industry for t in terms):
                add_group("factor", factor, code, v, "classification_proxy", source)
        if proxy.get("factor"):
            add_group("factor", proxy["factor"], code, v, "classification_proxy", source)
        if pos.get("is_etf"):
            comp = etf_components(code)
            e["lookthrough"] = comp
            for leaf, w in comp["weights"].items():
                add_group("underlying", leaf, code, v * w, "top10_only", comp["source"])
            add_group("underlying", "未知ETF余项", code, v * comp["unknown_frac"], "unknown", comp["source"])
        else:
            add_group("underlying", code, code, v, "direct", "snapshot")
    for key, g in groups.items():
        g["weight_frac"] = g["value"] / assets if assets and assets > 0 else None
        if g["dimension"] != "underlying" and g["label"] != "未知" and g["weight_frac"] is not None and g["weight_frac"] > cfg.get("group_weight_frac", 1):
            alerts.append(f"{key} 跨仓合计超过假设集中度上限")
    reserve = assets * cfg.get("cash_floor_frac", 0) if assets and assets > 0 else None
    if reserve is not None and cash is not None and cash < reserve:
        alerts.append("现金低于假设缓冲线")
    if assets and assets > 0 and known_loss / assets > cfg.get("portfolio_loss_frac", 1):
        alerts.append("已知估值情景损失超过假设组合预算；未知部分尚未计入")
    accounted = gross + cash if cash is not None else None
    if assets and accounted is not None and abs(accounted - assets) / assets > 0.01:
        alerts.append("快照持仓市值＋现金与券商总资产不一致，仓位/风险比例仅供参考")
    if not assets or assets <= 0 or cash is None:
        alerts.append("资产/现金缺失，无法可靠计算风险预算")
    if acc.get("updated") and str(acc["updated"])[:10] != str((snap.get("market") or {}).get("now", ""))[:10]:
        alerts.append("账户资产不是本日确认值，不自动写入净值台账")
    n = nav_report(load_nav()) if nav is None else nav
    stress = p.get("stress") or {}
    return {"mode": p.get("mode", "shadow"), "enforced": p.get("mode") == "active", "assumptions_only": True,
            "assets": assets, "cash": cash, "cash_reserve_yuan": reserve, "rows": rows, "groups": groups,
            "known_loss_yuan": known_loss, "unknown_valuation_value": unknown_value,
            "known_loss_frac": known_loss / assets if assets and assets > 0 else None,
            "market_value_missing": any(r.get("value") is None for r in rows.values()),
            "valuation_coverage_frac": max(0, 1 - unknown_value / gross) if gross > 0 and all(r.get("value") is not None for r in rows.values()) else None,
            "stress": {"market_loss_yuan": gross * stress.get("market_down_frac", 0),
                       "group_losses_yuan": {k: g["value"] * stress.get("group_down_frac", 0) for k, g in groups.items() if g["dimension"] in ("industry", "factor") and g["label"] != "未知"},
                       "note": "每个行业/因子场景独立，不能相加重复计算；跳空/跌停/T+1可能延迟执行"},
            "nav": n, "drawdown": drawdown_state(n, p), "limits": cfg, "alerts": alerts}


def buy_capacity(code, report, price):
    """按风险预算给新增金额上限；未知风险返回0但shadow不覆盖正式数量。"""
    assets, cash = report.get("assets"), report.get("cash")
    row = report.get("rows", {}).get(str(code)) or {}
    down = row.get("downside_frac")
    cfg = report.get("limits") or {}
    if not assets or not price or cash is None or down is None or report.get("market_value_missing") or report.get("unknown_valuation_value", 0) > 0:
        return {"amount": 0, "reason": "存在未知估值/资产，风险预算无法完整判断"}
    room = max(0, cash - (report.get("cash_reserve_yuan") or 0))
    if down > 0:
        room = min(room, max(0, assets * cfg.get("single_loss_frac", 1) - (row.get("risk_loss_yuan") or 0)) / down,
                   max(0, assets * cfg.get("portfolio_loss_frac", 1) - report.get("known_loss_yuan", 0)) / down)
    for g in report.get("groups", {}).values():
        if str(code) in g["codes"] and g["dimension"] in ("industry", "factor") and g["label"] != "未知":
            room = min(room, max(0, assets * cfg.get("group_weight_frac", 1) - g["value"]))
    if report.get("drawdown", {}).get("block_add"):
        room = 0
    return {"amount": max(0, room), "reason": "单股/组合情景损失、集中度、现金及回撤联合约束"}


def reserve_buy(code, qty, price, report):
    """按顺序占用模拟风险/现金预算，不能假定卖出建议已经成交。"""
    amount = qty * price
    row = report.get("rows", {}).get(str(code))
    if not row or amount <= 0:
        return
    down = row.get("downside_frac") or 0
    report["cash"] -= amount
    report["known_loss_yuan"] += amount * down
    row["value"] += amount
    row["risk_loss_yuan"] = (row.get("risk_loss_yuan") or 0) + amount * down
    for g in report.get("groups", {}).values():
        if str(code) in g["codes"] and g["dimension"] in ("industry", "factor"):
            g["value"] += amount


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    if argv and argv[0] == "nav":
        p.add_argument("cmd")
        p.add_argument("--date", required=True)
        p.add_argument("--assets", required=True, type=float)
        p.add_argument("--external-flow", required=True, type=float)
        p.add_argument("--source", required=True)
        a = p.parse_args(argv)
        result = record_nav(a.date, a.assets, a.external_flow, a.source)
    else:
        p.add_argument("--snapshot", required=True)
        p.add_argument("--out")
        a = p.parse_args(argv)
        import core_value as cv
        import signal_store as st
        with open(a.snapshot, encoding="utf-8") as f:
            snap = json.load(f)
        params = st.load_params()
        result = assess_portfolio(snap, params, cv.load_core(params))
        if a.out:
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            with open(a.out, "x", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=1)
                f.write("\n")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
