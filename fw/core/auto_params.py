"""动态阈值 / EV / 仓位：引擎（signals_engine.py）和校准（calibrate.py）共用。

思路
----
* 阈值不再写死百分比，而是写成「该标的近 20 日日均真实波幅 ATR% 的 k 倍」（unit=atr），
  或无量纲的位置/量比（unit=raw）。k 由 calibrate.py 每天收盘后按近期回测的扣费后期望收益校准，
  写入 rules_auto.json；rules_params.json 的 auto 段只定上下限、单日限幅与仓位硬约束。
* 每条候选按「规则 × 品种(etf/stock) × 市场状态(strong/range/weak)」查回测统计（已做分层收缩），
  算扣手续费后的期望收益 EV；EV ≤ 0 的候选屏蔽（status=ev_negative），止损不屏蔽只标注。
* 仓位：1/4 凯利（f = kelly_frac × μ / σ²）给建议金额，再套单笔上限、单只仓位上限、现金底线（两仓制下为 0）、
  交易仓预算（sleeves.trade.budget_frac × 总资产 − 交易仓市值；超出时交易仓只卖不买，做T低吸不受此限）。
* 最低金额不写死，由 EV 反推：手续费有最低 5 元佣金，金额越小费率越高。保本金额 = EV 刚好为 0 的金额；
  有效金额 = 扣费后净赚 ≥ 手续费的金额。数量在上限内尽量做到有效金额；可操作金额连保本都不到的，EV ≤ 0 自然屏蔽。
* 两仓制（rules_params.json sleeves 段）：max_positions / dust_yuan 只约束长线仓（长线仓的买卖由 core_value.py 给出，
  盘中只做T）；交易仓不限只数，只受预算约束。市值 < dust_yuan 的长线仓碎仓出减仓信号时改为一次清掉。
* EV 门槛：auto.sizing.min_ev_yuan（当前 20 元，交易仓另有 3% 目标价差，见 signals_engine.apply_move_target）。按凯利/上限定好数量后，单笔 EV（元）低于门槛的屏蔽
  （status=ev_negative，reason 写出达到门槛需要的金额）；门槛只做过滤，不用来放大数量。止损、仓位超上限的减仓除外。
* 做T：按 1/4 凯利定金额（下限为有效金额），上限为可卖数量（低吸另受仓位/现金约束），不受 t.qty_frac 限制。
rules_auto.json 缺失、过期或 auto.enabled=false 时，全部回退到 rules_params.json 的固定值。
"""
import copy
import datetime
import json
import math
import os

import fees

from fw.paths import ROOT
from fw.datamul import data_path

# name: (section, key, unit, sign)
TUNABLES = {
    "t_amp_k": ("t", "min_amplitude_pct", "atr", 1),
    "t_high_pos": ("t", "high_pos", "raw", 1),
    "t_low_pos": ("t", "low_pos", "raw", 1),
    "t_reb_k": ("t", "low_rebound_pct", "atr", 1),
    "t_pair_k": ("t", "pair_min_pct", "atr", 1),
    "add_reb_k": ("add", "rebound_pct", "atr", 1),
    "add_sup_k": ("add", "support_near_pct", "atr", 1),
    "add_drop_k": ("add", "drop_trigger_pct", "atr", -1),
    "red_reb_k": ("reduce", "rebound_pct", "atr", 1),
    "red_res_k": ("reduce", "resist_near_pct", "atr", 1),
    "wd_reb_k": ("watch", "dip_rebound_pct", "atr", 1),
    "wd_sup_k": ("watch", "dip_support_near_pct", "atr", 1),
    "wb_min_k": ("watch", "break_min_pct", "atr", 1),
    "wb_vol": ("watch", "break_vol_ratio", "raw", 1),
    "wb_pos": ("watch", "break_pos", "raw", 1),
}

# 校准分组：同组参数一起搜索，目标是组内规则扣费后收益之和
FAMILIES = {
    "t": {"rules": ["t_high_range_top", "t_low_rebound"], "role": "t",
          "params": ["t_amp_k", "t_high_pos", "t_low_pos", "t_reb_k", "t_pair_k"]},
    "add": {"rules": ["add_rebound_support"], "role": "add", "params": ["add_reb_k", "add_sup_k", "add_drop_k"]},
    "reduce": {"rules": ["reduce_rebound_resist"], "role": "reduce", "params": ["red_reb_k", "red_res_k"]},
    "watch_dip": {"rules": ["watch_dip_support"], "role": "watch", "params": ["wd_reb_k", "wd_sup_k"]},
    "watch_break": {"rules": ["watch_break_high20"], "role": "watch", "params": ["wb_min_k", "wb_vol", "wb_pos"]},
}

# 每条规则的收益口径与手续费腿数（见 calibrate.outcome）
RULE_LEGS = {
    "t_high_range_top": "round", "t_low_rebound": "round",
    "add_rebound_support": "round", "watch_dip_support": "round", "watch_break_high20": "round",
    "reduce_rebound_resist": "sell", "stop_new_low20": "sell", "stop_level": "sell",
}
BUY_TYPES = {"add", "watch_dip", "watch_break", "t_low"}
SELL_TYPES = {"reduce", "t_high", "stop"}
REGIMES = ("strong", "range", "weak")
REGIME_NAME = {"strong": "强势", "range": "震荡", "weak": "弱势"}
REGIME_MIN_N = 20   # 当前市场状态在该品种下的回测样本少于此数时，EV 只标注不屏蔽（统计来自别的市场状态）


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------- 加载
def auto_cfg(params):
    return params.get("auto") or {}


def auto_path(params):
    return data_path(auto_cfg(params).get("file", "rules_auto.json"))


def load_auto(params, today=None):
    """读 rules_auto.json；未启用、缺失或过期时返回 None（引擎回退固定阈值）。"""
    cfg = auto_cfg(params)
    if not cfg.get("enabled"):
        return None
    try:
        with open(auto_path(params), encoding="utf-8") as f:
            auto = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    today = today or datetime.date.today().strftime("%Y%m%d")
    asof = str((auto.get("data") or {}).get("to") or "")
    try:
        age = (datetime.datetime.strptime(today, "%Y%m%d") - datetime.datetime.strptime(asof, "%Y%m%d")).days
    except ValueError:
        return None
    if age > cfg.get("max_stale_days", 4):
        return None
    return auto


def regime_of(auto):
    return ((auto or {}).get("regime") or {}).get("next") or "range"


def ks_for(auto, regime):
    if not auto:
        return {}
    ks = dict((auto.get("ks") or {}).get("pooled") or {})
    ks.update(((auto.get("ks") or {}).get("by_regime") or {}).get(regime) or {})
    return ks


def atr_of(auto, code):
    return _f(((auto or {}).get("atr") or {}).get(str(code)))


# ----------------------------------------------------------------- 阈值
def effective(params, ks, atr):
    """把 k 值换算成具体阈值，返回新的 params（不修改入参）。atr 缺失时 atr 类参数保持固定值。"""
    p = copy.copy(params)      # 只复制被改动的段，回放时每个标的每天调用一次，避免整份深拷贝
    touched = set()
    for name, k in (ks or {}).items():
        spec = TUNABLES.get(name)
        if not spec or k is None:
            continue
        sec, key, unit, sign = spec
        if sec not in touched:
            p[sec] = dict(params[sec])
            touched.add(sec)
        if unit == "atr":
            if not atr:
                continue
            p[sec][key] = round(sign * k * atr, 3)
        else:
            p[sec][key] = round(k, 3)
    return p


def baseline_ks(params, median_atr):
    """把 rules_params.json 的固定阈值折算成 k（首次校准的起点）。"""
    out = {}
    for name, (sec, key, unit, sign) in TUNABLES.items():
        v = _f((params.get(sec) or {}).get(key))
        if v is None:
            continue
        out[name] = round(abs(v) / median_atr, 3) if unit == "atr" else v
    return out


# ----------------------------------------------------------------- EV
def stats_for(auto, rule, code, regime):
    """按 规则×品种×市场状态 → 规则×品种 → 规则 的顺序取已收缩的统计。港股借用 A 股个股统计。"""
    st = ((auto or {}).get("stats") or {}).get(rule) or {}
    kind = fees.kind_of(code)
    kind = "stock" if kind == "hk" else kind
    reg_n = (st.get(f"{kind}|{regime}") or {}).get("n") or 0
    broad = next((st[k] for k in (kind, "all") if (st.get(k) or {}).get("n")), None)
    for key in (f"{kind}|{regime}", kind, "all"):
        s = st.get(key)
        if s and s.get("n"):
            return dict(s, key=key, regime_n=reg_n, thin=reg_n < REGIME_MIN_N, broad=broad)
    return None


def ev_of(stats, amount, code, rule):
    """扣手续费后的期望收益：返回 (ev_pct, ev_yuan, fee_yuan)。"""
    legs = RULE_LEGS.get(rule, "round")
    fee_pct = fees.cost_pct(amount, code, legs) or 0.0
    ev_pct = stats["mean"] - fee_pct
    return ev_pct, ev_pct / 100 * amount, fee_pct / 100 * amount


def amount_for(stats, code, rule, need=0.0, hi=1e7, plus=0.0):
    """最小金额 a，使 EV(a) > need × 手续费(a) + plus。need=0 → 保本金额；need=1 → 有效金额（净赚 ≥ 手续费）；
    plus=min_ev_yuan → EV 达到最低门槛所需金额。
    EV(a) − need×费 = a×(μ − (1+need)×费率(a))，费率随金额单调不增，所以二分。任何金额都达不到时返回 None。"""
    def ok(a):
        ev_pct, _, fee_y = ev_of(stats, a, code, rule)
        return ev_pct / 100 * a > need * fee_y + plus
    if not ok(hi):
        return None
    lo = 1.0
    if ok(lo):
        return lo
    while hi - lo > 1:
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if ok(mid) else (mid, hi)
    return round(hi)


def kelly_amount(stats, total_assets, sz, fee_pct=0.0):
    """1/4 凯利金额：f = kelly_frac × μ / σ²（μ、σ 为单笔收益率）。"""
    mu = (stats["mean"] - fee_pct) / 100
    sd = max((stats.get("sd") or 0) / 100, 0.005)
    if mu <= 0 or not total_assets:
        return 0.0
    f = sz.get("kelly_frac", 0.25) * mu / (sd * sd)
    return min(f, sz.get("max_trade_frac", 0.05)) * total_assets


def _round_lot(q, lot):
    return int(q // lot * lot) if q and q > 0 else 0


def size_and_ev(c, ctx):
    """给候选算 EV 并按资金/仓位重新定数量。就地修改 c，返回 c。

    ctx: auto, regime, params, total_assets, cash, pos_value{code: 市值}, lot{code: 每手}, min_lot{code},
         n_positions（长线仓只数）, held（持仓代码集合）, dust（长线仓碎仓列表）,
         sleeves{code: core/trade}, trade_value（交易仓市值）, trade_budget（交易仓上限，元）
    止损：数量不动（全部可卖），EV 只作标注；异动/溢价：不处理。
    """
    t, rule, code = c.get("type"), c.get("rule"), c.get("code")
    if t not in BUY_TYPES | SELL_TYPES or not ctx.get("auto"):
        return c
    sz = auto_cfg(ctx["params"]).get("sizing") or {}
    price = _f(c.get("price"))
    stats = stats_for(ctx["auto"], rule, code, ctx["regime"])
    pv = ctx["pos_value"].get(code, 0.0)
    held = pv > 0 or code in (ctx.get("held") or set())
    sleeve = (ctx.get("sleeves") or {}).get(str(code), "trade")
    if not price or not stats:
        c.setdefault("flags", []).append("EV：无回测统计，按固定规则")
        return c
    TA = ctx.get("total_assets") or 0
    cash = ctx.get("cash") or 0
    lot = ctx["lot"].get(code, 100)
    min_q = ctx.get("min_lot", {}).get(code, lot)
    m = c.setdefault("metrics", {})
    avail = _f(m.get("available"))
    notes = []
    forced = False
    forced_why = ""
    # 长线仓碎仓（市值 < dust_yuan）减仓直接清掉；交易仓不看只数/碎仓
    dust_y = sz.get("dust_yuan", 10000)
    is_dust = held and sleeve == "core" and 0 < pv < dust_y

    if t == "stop":
        amt = (c.get("qty") or 0) * price
        ev_pct, ev_y, fee_y = ev_of(stats, amt or 10000, code, rule)
        c["ev"] = _ev_dict(stats, ev_pct, ev_y, fee_y, amt, ctx["regime"])
        if ev_pct <= 0:
            c.setdefault("flags", []).append(f"回测 EV {ev_pct:+.2f}%（止损是防守线，不因 EV 屏蔽）")
        return c

    # 最低金额由 EV 反推：be 保本、good 净赚 ≥ 手续费（数量尽量做到 good，摊薄最低 5 元佣金）
    # 当前市场状态样本少（thin）时，该层统计不可靠，定数量改用不分市场状态的合并统计
    ss = stats["broad"] if stats.get("thin") and stats.get("broad") else stats
    be = amount_for(ss, code, rule, 0)
    good = amount_for(ss, code, rule, 1)
    fee_ref = fees.cost_pct(max(kelly_amount(ss, TA, sz), good or 0, 1), code, RULE_LEGS.get(rule, "round")) or 0
    want = kelly_amount(ss, TA, sz, fee_ref)
    floor_amt = good or 0
    orig_amt = (c.get("qty") or 0) * price

    # 上限：卖出不超过可卖市值；买入不超过 单只仓位上限 与 现金底线 留出的空间
    if t in SELL_TYPES:
        cap = (avail or 0) * price
        over = pv - sz.get("position_cap", 0.2) * TA if TA else 0
        if t == "reduce" and over > 0:
            want = max(want, over)
            forced = True
            forced_why = "仓位超单只上限，减到上限是硬约束"
            notes.append(f"仓位 {pv / TA:.0%} 超过上限 {sz.get('position_cap', 0.2):.0%}，至少减到上限")
        if t == "reduce" and is_dust:
            want = max(want, cap)
            notes.append(f"碎仓（市值 {pv:.0f} 元 < {dust_y:.0f} 元），建议一次清掉")
    else:
        room_pos = sz.get("position_cap", 0.2) * TA - pv if TA else float("inf")
        room_cash = cash - sz.get("cash_floor", 0.2) * TA if TA else cash
        room_trade = float("inf")
        if t == "t_low":
            room_pos = float("inf")   # 做T低吸当天/次日卖回同数量，仓位不累积，也不占交易仓预算
        elif sleeve != "core" and ctx.get("trade_budget") is not None:
            room_trade = ctx["trade_budget"] - (ctx.get("trade_value") or 0)
        cap = max(0.0, min(room_pos, room_cash, room_trade))
        if cap < min_q * price:
            c["status"] = "ev_negative"
            lim_ = min(room_pos, room_cash, room_trade)
            why = ("交易仓已到预算上限" if lim_ == room_trade else "现金不足" if lim_ == room_cash else "仓位已到单只上限")
            c["reason"] += (f"；{why}（总资产 {TA:.0f}，现金 {cash:.0f}，该标的市值 {pv:.0f}"
                            + (f"，交易仓 {ctx.get('trade_value') or 0:.0f}/{ctx['trade_budget']:.0f}" if room_trade != float("inf") else "")
                            + "），不再买入")
            c["ev"] = _ev_dict(stats, None, None, None, 0, ctx["regime"], be, good)
            return c

    # 做T：按凯利定金额，只受可卖数量（低吸另受仓位/现金）这些硬上限约束，不受规则原数量 t.qty_frac 限制；
    # 减仓：凯利只用来缩量，不放大到规则数量以上（最多放大到有效金额 good，摊薄最低佣金）；
    # 做T低吸要当天/次日卖回同数量的老仓，数量也不超过可卖数量
    hard_cap = cap if t not in ("t_low",) else min(cap, (avail or 0) * price)   # 可卖/仓位/现金的硬上限
    if t in ("t_high", "t_low"):
        cap = hard_cap
    elif t == "reduce":
        lim = max(orig_amt, floor_amt)
        if t == "reduce" and TA and pv - sz.get("position_cap", 0.2) * TA > 0:
            lim = max(lim, pv - sz.get("position_cap", 0.2) * TA)
        if t == "reduce" and is_dust:
            lim = max(lim, (avail or 0) * price)
        cap = min(cap, lim)
    if not good and not want:
        amount = min(orig_amt, cap) if cap else 0   # 任何金额都不保本：按规则原数量算出 EV，给出屏蔽原因
    else:
        amount = min(max(want, floor_amt), cap) if cap else 0
    q = _round_lot(amount / price, lot)
    if floor_amt and q * price < floor_amt and (q + lot) * price <= hard_cap + 1e-6:
        q += lot   # 向下取整后低于有效金额时补一手
    if t not in SELL_TYPES and q < min_q and cap >= min_q * price:
        q = min_q
    if t in SELL_TYPES or t == "t_low":
        q = min(q, int(avail or 0))
    if q <= 0:
        c["status"] = "ev_negative"
        c["reason"] += "；可操作数量为 0"
        return c
    amount = q * price
    ev_pct, ev_y, fee_y = ev_of(stats, amount, code, rule)
    if t in ("t_high", "t_low") and orig_amt and amount > orig_amt * 1.01:
        notes.append(f"按凯利加大到约 {amount:.0f} 元（规则原数量约 {orig_amt:.0f} 元，上限为可卖数量）")
    if t == "t_high" and avail and q >= 0.9 * avail:
        notes.append("做T数量接近全部可卖：回补腿没到价就等于这部分仓位卖掉了")
    elif t == "reduce" and orig_amt and amount > orig_amt * 1.01 and not is_dust:
        notes.append(f"加大到有效金额约 {good:.0f} 元以摊薄最低佣金" if good else "加大数量以摊薄手续费")
    old_q = c.get("qty")
    c["qty"] = q
    c["ev"] = _ev_dict(stats, ev_pct, ev_y, fee_y, amount, ctx["regime"], be, good)
    c["reason"] += (f"；EV {ev_pct:+.2f}%≈{ev_y:+.0f} 元（胜率 {stats.get('win', 0):.0%}，样本 {stats['n']}，"
                    f"{REGIME_NAME.get(ctx['regime'], ctx['regime'])}），数量 {old_q}→{q}（约 {amount:.0f} 元，手续费约 {fee_y:.1f} 元）")
    if t.startswith("t_") and c.get("pair_zone"):
        c["reason"] = c["reason"].replace(f"接回 {old_q}", f"接回 {q}").replace(f"卖出 {old_q}", f"卖出 {q}")
    if notes:
        c.setdefault("flags", []).extend(notes)
    if ev_pct <= 0 and forced:
        c.setdefault("flags", []).append(f"回测 EV {ev_pct:+.2f}%，但{forced_why}，不屏蔽")
    elif ev_pct <= 0 and stats.get("thin") and stats.get("broad") and ev_of(stats["broad"], amount, code, rule)[0] <= 0:
        # 当前市场状态样本少，但不分市场状态合并看扣费后也 ≤ 0：是手续费吃掉了收益，与样本多少无关
        c["status"] = "ev_negative"
        c["reason"] += (f"；扣手续费后 EV ≤ 0（当前市场状态样本少，但合并各市场状态的毛收益 "
                        f"{stats['broad']['mean']:+.2f}% 也抵不过手续费 {fee_y / amount * 100:.2f}%），屏蔽")
    elif ev_pct <= 0 and stats.get("thin"):
        c.setdefault("flags", []).append(
            f"EV {ev_pct:+.2f}% 但{REGIME_NAME.get(ctx['regime'], ctx['regime'])}市回测样本仅 {stats.get('regime_n', 0)} 条"
            f"（统计主要来自其他市场状态），不屏蔽，仅供参考")
    elif ev_pct <= 0:
        c["status"] = "ev_negative"
        if be is None:
            c["reason"] += "；回测毛收益在任何金额下都抵不过手续费，EV ≤ 0，屏蔽"
        elif amount < be:
            c["reason"] += f"；可操作金额约 {amount:.0f} 元，低于保本金额 {be:.0f} 元（最低 5 元佣金摊不薄），EV ≤ 0，屏蔽"
        else:
            c["reason"] += "；扣手续费后 EV ≤ 0，屏蔽"
    min_ev = sz.get("min_ev_yuan") or 0
    ev_chk = ev_of(ss, amount, code, rule)[1]   # 样本少时用合并统计判断
    if min_ev and c.get("status") == "new" and not forced and ev_chk < min_ev:
        need_amt = amount_for(ss, code, rule, 0, plus=min_ev)
        c["status"] = "ev_negative"
        c["reason"] += (f"；EV 约 {ev_chk:+.0f} 元，低于门槛 {min_ev:.0f} 元"
                        + (f"（按单笔毛收益 {ss['mean']:+.2f}% 需做约 {need_amt:.0f} 元，超出可操作金额）" if need_amt
                           else "（任何金额都达不到）") + "，不操作")
    elif min_ev and forced and ev_chk < min_ev and c.get("status") == "new":
        c.setdefault("flags", []).append(f"EV 约 {ev_chk:+.0f} 元低于门槛 {min_ev:.0f} 元，但{forced_why}，不屏蔽")
    return c


def _ev_dict(stats, ev_pct, ev_y, fee_y, amount, regime, be=None, good=None):
    r = lambda x, n=2: None if x is None else round(x, n)
    return {"ev_pct": r(ev_pct), "ev_yuan": r(ev_y, 1), "fee_yuan": r(fee_y, 1), "amount": r(amount, 0),
            "breakeven_yuan": r(be, 0), "efficient_yuan": r(good, 0),
            "mean_gross_pct": r(stats.get("mean")), "sd_pct": r(stats.get("sd")), "win": r(stats.get("win")),
            "n": stats.get("n"), "basis": stats.get("key"), "regime": regime,
            "regime_n": stats.get("regime_n"), "thin": stats.get("thin")}
