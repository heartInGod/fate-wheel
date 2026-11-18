#!/usr/bin/env python3
"""C：长线价格防守隔离对照。只读本地日K，不改正式参数/持仓/信号。

python3 main.py risk-exp --out reports/20261112_risk_experiment.json
比较 hold / trend / profit / time / combined；t日收盘判断，最早t+1开盘模拟成交。
当前目标价不回填历史，不把价格实验当完整长线策略回测；默认不推荐上线。
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import statistics

import fees
import portfolio_risk as pr

from fw.paths import ROOT
STRATEGIES = ("hold", "trend", "profit", "time", "combined")


def normalize_bars(rows, asof=None):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("日K必须为记录列表")
    if asof:
        dt.date.fromisoformat(asof)
    bars, seen = [], set()
    for row in rows:
        date = row.get("date")
        try:
            dt.date.fromisoformat(date)
        except (ValueError, TypeError):
            raise ValueError("日K日期错误")
        if asof and date > asof:
            continue
        if date in seen:
            raise ValueError("日K日期重复")
        seen.add(date)
        o, c, h, l = (pr.number(row.get(k)) for k in ("open", "close", "high", "low"))
        if any(v is None or v <= 0 for v in (o, c, h, l)) or l > min(o, c) or h < max(o, c):
            raise ValueError("日K价格缺失或OHLC不一致")
        bars.append(dict(row, date=date, open=o, close=c, high=h, low=l))
    if any(bars[i]["date"] >= bars[i + 1]["date"] for i in range(len(bars) - 1)):
        raise ValueError("日K未按日期升序")
    return bars


def blocked(bar, previous, side, code):
    if bar.get("tradeable") is False or pr.number(bar.get("volume")) == 0:
        return True
    if previous and abs(bar["high"] - bar["low"]) < 1e-8:
        rate = pr.number(bar.get("limit_pct")) or (0.2 if str(code).startswith(("300", "301", "688")) else 0.1)
        change = bar["open"] / previous["close"] - 1
        if side < 0 and change <= -rate + 0.005 or side > 0 and change >= rate - 0.005:
            return True
    return False


def validate_experiments(policy):
    """实验配置也必须完整合法；缺项/NaN/字符串不靠隐式默认继续跑。"""
    if policy.get("errors"):
        raise ValueError("；".join(policy["errors"]))
    e = policy.get("experiments")
    if not isinstance(e, dict):
        raise ValueError("缺实验参数")
    def value(section, key):
        cfg = e if section is None else e.get(section)
        raw = cfg.get(key) if isinstance(cfg, dict) else None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise ValueError(f"实验参数 {section or 'experiments'}.{key} 必须为有限数值")
        return raw
    for section, key in (("trend", "ma_days"), ("trend", "confirm_days"), ("trend", "recover_days"),
                         ("time", "review_days"), (None, "min_observation_days")):
        n = value(section, key)
        if n < 1 or n != int(n):
            raise ValueError(f"实验天数 {key} 必须为正整数")
    for section, key in (("trend", "exit_fraction"), ("profit", "exit_fraction"), ("time", "exit_fraction"),
                         ("profit", "trailing_drop_frac")):
        if not 0 < value(section, key) <= 1:
            raise ValueError(f"实验比例 {key} 必须在(0,1]内")
    if value("profit", "activate_gain_frac") < 0 or not -1 < value("time", "min_return_frac") <= 1:
        raise ValueError("盈利激活/时间收益参数错误")
    if not 0 <= value(None, "slippage_bps") < 1000:
        raise ValueError("滑点必须在[0,1000)bps内")
    return e


def simulate(code, bars, strategy, policy, capital=100000):
    if strategy not in STRATEGIES:
        raise ValueError("未知实验策略")
    e = validate_experiments(policy)
    bars = normalize_bars(bars)
    trend, profit, time = e["trend"], e["profit"], e["time"]
    warmup = int(trend["ma_days"])
    if len(bars) <= warmup + 1:
        return {"available": False, "reason": "MA预热与后续样本不足"}
    slip = e.get("slippage_bps", 0) / 10000
    if not 0 <= slip < 0.1 or capital <= 0:
        raise ValueError("资金或滑点错误")
    lot = 200 if str(code).startswith("688") else 100
    cash, shares, base_qty, basis, peak, nav_peak, max_dd = capital, 0, 0, None, 0, capital, 0
    below, above, entry_i = 0, 0, None
    fired, trades, pending, history = set(), [], {"target": None, "reason": "initial_buy", "signal_date": bars[warmup - 1]["date"]}, []
    blocked_n, fee_total, turnover = 0, 0.0, 0.0
    active_trim, caps = False, {}
    cancelled_plans = []
    def affordable(price, max_qty):
        q = int(min(max_qty, cash / price) // lot * lot)
        while q > 0 and q * price + fees.one_way(q * price, code, 1) > cash:
            q -= lot
        return max(0, q)
    for i in range(warmup, len(bars)):
        bar = bars[i]
        prev = bars[i - 1]
        if pending is not None:
            target = pending["target"]
            side = 1 if target is None or target > shares else -1
            if blocked(bar, prev, side, code):
                blocked_n += 1  # 保留未执行风险，不假装已经成交
            else:
                execution_price = bar["open"] * (1 + slip * side)
                desired = capital / execution_price if target is None else abs(target - shares)
                qty = affordable(execution_price, desired) if side > 0 else min(shares, int(desired))
                if qty > 0:
                    amount = qty * execution_price
                    fee = fees.one_way(amount, code, side)
                    cash += amount - fee if side < 0 else -amount - fee
                    shares += side * qty
                    fee_total += fee
                    turnover += amount
                    trades.append({"signal_date": pending["signal_date"], "date": bar["date"], "side": side,
                                   "qty": qty, "price": execution_price, "fee": fee, "reason": pending["reason"], "index": i})
                    if basis is None:
                        basis, base_qty, entry_i = execution_price, shares, i
                        peak = basis
                if target is None or target == shares:
                    pending = None
                elif qty == 0:
                    pending["blocked_reason"] = "可用现金不足以完成恢复买入"
        value = cash + shares * bar["close"]
        nav_peak = max(nav_peak, value)
        dd = 1 - value / nav_peak
        max_dd = max(max_dd, dd)
        history.append({"date": bar["date"], "nav": value / capital, "drawdown_frac": dd, "shares": shares, "cash": cash})
        if basis is None:
            if pending is None:
                pending = {"target": None, "reason": "initial_buy", "signal_date": bar["date"]}
            continue
        # 所有信号只用本日收盘及之前数据，不使用明日开盘/未来高点。
        close = bar["close"]
        ma = sum(b["close"] for b in bars[i - warmup:i]) / warmup
        below = below + 1 if close < ma else 0
        above = above + 1 if close > ma else 0
        peak = max(peak, close)
        if strategy in ("trend", "combined"):
            if below >= trend["confirm_days"]:
                active_trim = True
            elif above >= trend["recover_days"]:
                active_trim = False
        if strategy in ("profit", "combined") and "profit" not in fired:
            if peak / basis - 1 >= profit["activate_gain_frac"] and close <= peak * (1 - profit["trailing_drop_frac"]):
                caps["profit_protect"] = int(base_qty * (1 - profit["exit_fraction"]) // lot * lot)
                fired.add("profit")
        if strategy in ("time", "combined") and "time" not in fired:
            if i - entry_i >= time["review_days"]:
                fired.add("time")
                if close / basis - 1 < time["min_return_frac"]:
                    caps["time_review_trim"] = int(base_qty * (1 - time["exit_fraction"]) // lot * lot)
        # 所有防守目标持续保留；新硬保护可以收紧旧pending，不因执行受阻丢掉。
        choices = [(base_qty, "trend_recovery")] + [(q, reason) for reason, q in caps.items()]
        if active_trim:
            choices.append((int(base_qty * (1 - trend["exit_fraction"]) // lot * lot), "trend_defense"))
        target, reason = min(choices, key=lambda x: x[0])
        if target == shares:
            if pending is not None:
                cancelled_plans.append(dict(pending, cancelled_on=bar["date"], reason_for_cancel="收盘确认目标已恢复或已满足"))
            pending = None
        elif pending is None or pending["target"] != target:
            if pending is not None:
                cancelled_plans.append(dict(pending, cancelled_on=bar["date"], reason_for_cancel="收盘确认目标变化"))
            pending = {"target": target, "reason": reason, "signal_date": bar["date"]}
    sells = [t for t in trades if t["side"] < 0]
    assessable, fly = 0, 0
    for t in sells:
        future = bars[t["index"] + 1:t["index"] + 21]
        if len(future) == 20:
            assessable += 1
            fly += max(b["close"] for b in future) > t["price"]
    return {"available": True, "net_return_frac": history[-1]["nav"] - 1, "max_drawdown_frac": max_dd,
            "trade_count": len(trades), "sell_count": len(sells), "fee_yuan": fee_total,
            "turnover_frac": turnover / capital, "sell_fly_rate_20d": fly / assessable if assessable else None,
            "sell_fly_samples": assessable, "observed_days": len(history), "start": history[0]["date"], "end": history[-1]["date"],
            "blocked_execution_days": blocked_n, "pending_at_end": pending, "cancelled_plans": cancelled_plans,
            "signal_method": "t日收盘与之前60日（按ma_days）收盘均线比较；t+1开盘执行",
            "trades": trades, "history": history, "terminal_value_method": "剩余持仓按最后收盘价计值；不假设已清仓"}


def compare(code, rows, policy, asof=None):
    bars = normalize_bars(rows, asof)
    results = {s: simulate(code, bars, s, policy) for s in STRATEGIES}
    baseline = results["hold"]
    for s, r in results.items():
        if r.get("available") and baseline.get("available"):
            r["return_delta_vs_hold"] = r["net_return_frac"] - baseline["net_return_frac"]
            r["drawdown_improvement_vs_hold"] = baseline["max_drawdown_frac"] - r["max_drawdown_frac"]
    return results


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--codes", help="逗号分隔；默认只取当前长线归属的六位代码")
    p.add_argument("--asof", default=dt.date.today().isoformat())
    p.add_argument("--out")
    a = p.parse_args(argv)
    policy = pr.load_policy()
    try:
        validate_experiments(policy)
        dt.date.fromisoformat(a.asof)
    except (ValueError, TypeError) as exc:
        p.error(str(exc))
    import core_value as cv
    import signal_store as st
    core = cv.load_core(st.load_params())
    codes = a.codes.split(",") if a.codes else [c for c, r in (core.get("sleeves") or {}).items() if r.get("sleeve") == "core" and len(c) == 6]
    codes = list(dict.fromkeys(c.strip() for c in codes))
    if any(not re.fullmatch(r"[0-9]{6}", c) for c in codes):
        p.error("实验代码必须为六位数字，仅允许当前工作区日K缓存")
    results, errors, data_sources = {}, [], {}
    for code in codes:
        path = os.path.join(ROOT, "cache", "calib", "day", code + ".json")
        try:
            with open(path, encoding="utf-8") as f:
                rows = json.load(f)
            results[code] = compare(code, rows, policy, a.asof)
            data_sources[code] = {"path": path, "price_adjustment": "unknown", "selection": "当前持仓样本（有幸存者偏差）"}
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"{code} 本地日K不可用：{exc}")
    summary = {}
    for strategy in STRATEGIES:
        rs = [r[strategy] for r in results.values() if r[strategy].get("available")]
        summary[strategy] = {"n": len(rs), "mean_net_return_frac": statistics.mean(r["net_return_frac"] for r in rs) if rs else None,
                             "mean_max_drawdown_frac": statistics.mean(r["max_drawdown_frac"] for r in rs) if rs else None,
                             "total_trades": sum(r["trade_count"] for r in rs), "fee_yuan": sum(r["fee_yuan"] for r in rs),
                             "mean_return_delta_vs_hold": statistics.mean(r["return_delta_vs_hold"] for r in rs) if rs else None,
                             "mean_drawdown_improvement": statistics.mean(r["drawdown_improvement_vs_hold"] for r in rs) if rs else None,
                             "min_observed_days": min((r["observed_days"] for r in rs), default=0)}
    reasons = ["缓存价格复权口径未确认", "仅当前持仓样本，存在幸存者偏差", "价格防守实验不是完整长线估值/基本面策略回测",
               "一字涨跌停用全天OHLC保守判定可成交性；无逐笔/盘口证据，开盘成交只是模拟假设"]
    if errors or any(not r["hold"].get("available") for r in results.values()):
        reasons.append("部分标的缺缓存或有效样本，不把可用子样本当完整覆盖")
    minimum = policy["experiments"].get("min_observation_days", 252)
    if not results or any(r["min_observed_days"] < minimum for r in summary.values()):
        reasons.append(f"有效观察期不足{minimum}个交易日，不足以决定长期策略启用")
    report = {"asof": a.asof, "mode": "isolated_experiment", "activated": False, "eligible_for_activation": False,
              "ineligible_reasons": reasons, "parameters": policy["experiments"], "sources": data_sources,
              "summary_metric": "各标的独立模拟的等权平均；不是真实账户/组合收益", "summary": summary,
              "results": results, "errors": errors, "disclaimer": "以上为信息整理，不构成投资建议"}
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "x", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
            f.write("\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False, indent=1))
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
