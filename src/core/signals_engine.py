#!/usr/bin/env python3
"""规则引擎：把 intraday_rules.md 里的定性规则量化，由 Python 先算出候选信号，大模型再判断。

用法：
  python3 main.py signals                       # 读 cache/snapshot_intraday.json，输出候选并记录
  python3 main.py signals --snapshot cache/snapshots/20261030_1415.json
  python3 main.py signals --no-record           # 只看不记录
  python3 main.py signals --mode premarket      # 盘前：只输出各标的支撑/压力位与角色，不出信号
  python3 main.py signals --source catchup      # 记录来源（engine/catchup/backfill）

输出 JSON：
  candidates   新候选（status=new），大模型逐条判断 accept / reject
  suppressed   被屏蔽的候选（blocked=溢价超限等；group_suppressed=二选一里分数较低的一只；
               capped=自选股本时点超过 max_per_slot 条，不记录；ev_negative=EV≤0、EV<min_ev_yuan 或
               仓位/现金/交易仓预算到顶；买入类上方 20/60 日高点空间 < sleeves.trade.min_move_pct 也标 blocked），供参考
  portfolio    两仓：长线仓只数/上限/碎仓（< dust_yuan）/偏小仓位（< min_position_yuan）；交易仓市值、
               预算（总资产×sleeves.trade.budget_frac）、剩余空间，超预算时只卖不买
  open_trades  已采纳、带 tp_price 的交易仓买入：take_profit（到 +3% 止盈价）/ expire（满 hold_days 天）/ hold
  自选股（snapshot.watchlist，未持有）只出 watch_dip / watch_break 两类买点，带 watch=true
  duplicates   与今天已有信号重复（同标的同方向、价格变化 < dedup.price_change_pct（3%）），不需要再判断
  market_pulse 大盘资金脉搏（宽基 ETF 量能 + 政策快讯）：level=strong/watch/normal/none，只作背景；
               strong/watch 时卖出类候选带 flag「疑似国家队护盘」，由大模型决定是否推迟或放宽
阈值：rules_params.json 为固定值；auto.enabled 且 rules_auto.json 未过期时，百分比阈值改为
「该标的 ATR20% × k」（k 由 calibrate.py 每天按回测校准，按市场状态分档），见 auto_params.py。
EV / 仓位：每条买卖候选带 ev（扣手续费后的期望收益，来自回测+实盘统计），数量按 1/4 凯利并受
单只仓位上限、现金底线约束；EV≤0 或已无仓位/现金空间的标 ev_negative 放进 suppressed（止损只标注）。
两仓制（rules_params.json sleeves 段）：持仓按 core_auto.json 分为长线仓 core / 交易仓 trade（core_value.py 每周复查）。
长线仓盘中只做T（两腿价差 ≥ 3%），买卖由估值目标价决定；交易仓走全部规则，不限只数、受 20% 预算约束，
买入类候选附 tp_price（+3%）与 hold_days（5）。只数上限 max_positions 与碎仓只看长线仓；碎仓减仓改为清仓。
--mode premarket 额外输出 core（core_value.status：长线仓分区与目标价）与 open_trades。
信号与判断记录在 data/report/signals/yyyyMMdd.jsonl（见 signal_store.py）。
"""
import datetime
import json
import math
import os
import re
import sys

import auto_params as ap
import core_value as cv
import signal_store as st

from paths import ROOT
SNAP_LIVE = os.path.join(ROOT, "cache", "snapshot_intraday.json")
SNAP_PRE = os.path.join(ROOT, "cache", "snapshot_premarket.json")
SNAP_DIR = os.path.join(ROOT, "cache", "snapshots")

SLOT_LIST = [
    "09:30", "09:40", "09:50", "10:00", "10:10", "10:20", "10:30", "10:40", "10:50",
    "11:00", "11:10", "11:20", "11:30",
    "13:00", "13:10", "13:20", "13:30", "13:40", "13:50", "14:00", "14:10", "14:20", "14:30", "14:40", "14:50",
    "15:00",
]


# ----------------------------------------------------------------- 工具
def _market_regime(snap, params):
    """大盘资金面分档（宽度闸门），模块不可用时返回 None，不影响信号。"""
    try:
        from market_regime import classify
        reg = classify((snap or {}).get("market_flow") or {},
                       (params or {}).get("market_regime") or {})
        return {k: reg.get(k) for k in ("regime", "label", "gate", "evidence", "crosses")}
    except Exception:  # noqa: BLE001
        return None


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _pct(a, b):
    """a 相对 b 的百分比变化。"""
    if a is None or not b:
        return None
    return (a / b - 1) * 100


def _rp(x, price):
    """按价格量级取小数位。"""
    if x is None:
        return None
    nd = 3 if (price or 0) < 10 else 2
    return round(x, nd)


def slot_of(snap):
    if snap.get("_slot"):
        return snap["_slot"]
    ts = (snap.get("market") or {}).get("now") or snap.get("generated_at") or ""
    m = re.search(r"(\d{1,2}):(\d{2})", str(ts))
    if not m:
        return datetime.datetime.now().strftime("%H:%M")
    cur = int(m.group(1)) * 60 + int(m.group(2))
    best = SLOT_LIST[0]
    for s in SLOT_LIST:  # 取不晚于当前时间的最后一个时点
        h, mm = map(int, s.split(":"))
        if h * 60 + mm <= cur + 2:  # 允许任务提前 2 分钟触发
            best = s
    return best


def day_of(snap):
    ts = (snap.get("market") or {}).get("now") or snap.get("generated_at") or ""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(ts))
    if m:
        return "".join(m.groups())
    return datetime.date.today().strftime("%Y%m%d")


def prev_snapshot(day, slot):
    """上一个时点的归档快照（用于和上一时点相比的变化，盘中间隔 10 分钟）。"""
    if slot not in SLOT_LIST:
        return None
    i = SLOT_LIST.index(slot)
    for s in reversed(SLOT_LIST[:i]):
        p = os.path.join(SNAP_DIR, f"{day}_{s.replace(':', '')}.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return None
    return None


def roles_of(pos, params):
    stance = pos.get("stance") or ""
    kw = params["role_keywords"]
    roles = set()
    for r in ("reduce", "t", "add"):
        if any(k in stance for k in kw.get(r, [])):
            roles.add(r)
    if "不加" in stance:
        roles.discard("add")
    plain = re.sub(r"不(?:减仓|清仓|减)", "", stance)   # 「目标价前不减仓」这类否定不算减仓角色
    if not any(k in plain for k in kw.get("reduce", [])):
        roles.discard("reduce")
    cfg = params.get("codes", {}).get(pos.get("code"), {})
    if cfg.get("roles"):
        roles = set(cfg["roles"])
    return roles


def code_cfg(pos, params):
    cfg = dict(params.get("codes", {}).get(pos.get("code"), {}))
    stance = pos.get("stance") or ""
    if "premium_max" not in cfg:
        m = re.search(r"溢价率?\s*>\s*(\d+(?:\.\d+)?)%", stance)
        if m:
            cfg["premium_max"] = float(m.group(1))
    if "group" not in cfg:
        m = re.search(r"与\s*(\d{6})\s*二选一", stance)
        if m:
            cfg["group"] = "-".join(sorted([pos.get("code"), m.group(1)]))
    return cfg


def lot_of(pos, params, cfg):
    if cfg.get("lot"):
        return int(cfg["lot"])
    return int(params["lot"]["HK" if pos.get("market") == "hk" else "A"])


def round_lot(q, lot):
    return int(q // lot * lot) if q else 0


def trade_cfg(params):
    return (params.get("sleeves") or {}).get("trade") or {}


def apply_move_target(c, price, tech, params):
    """交易仓买入类（加仓/自选低吸/自选突破）：要有 ≥ min_move_pct 的目标价差。
    上方最近的 20/60 日高点距现价不足 min_move_pct 的标 blocked；否则附止盈价 tp_price 与最长持有天数。"""
    tc = trade_cfg(params)
    mm = _f(tc.get("min_move_pct")) or 0
    if mm <= 0:
        return c
    hold = tc.get("hold_days", 5)
    res = [(n, _f(tech.get(k))) for n, k in (("20日高", "high20"), ("60日高", "high60"))]
    res = [(n, v) for n, v in res if v and v > price]
    if res:
        n, r = min(res, key=lambda x: x[1])
        room = _pct(r, price)
        c.setdefault("metrics", {})["upside_room_pct"] = round(room, 2)
        if room < mm:
            c["status"] = "blocked"
            c["reason"] += f"；上方{n} {r:g} 距现价仅 {room:.1f}%，不足 {mm:g}% 目标价差，不买"
            return c
    c["tp_price"] = _rp(price * (1 + mm / 100), price)
    c["hold_days"] = hold
    c["reason"] += f"；止盈 {c['tp_price']}（+{mm:g}%），最多持有 {hold} 个交易日，到期按收盘价卖出"
    return c


# ----------------------------------------------------------------- 规则
def evaluate_position(pos, params, cash, prev_pos=None, sleeve="trade"):
    """对单只持仓计算候选信号，返回 list[dict]（未做去重）。
    sleeve="core"（长线仓）：只做T（按交易仓规则）与 stop_levels 防守线，不出加仓/反弹减仓/创新低止损。"""
    price, low, high = _f(pos.get("price")), _f(pos.get("low")), _f(pos.get("high"))
    prev_close = _f(pos.get("prev_close"))
    if not price or not low or not high or pos.get("is_today") is False:
        return []
    tech = pos.get("tech") or {}
    roles = roles_of(pos, params)
    if sleeve == "core":
        roles = {"t"}
    cfg = code_cfg(pos, params)
    lot = lot_of(pos, params, cfg)
    avail = _f(pos.get("available")) or 0
    shares = _f(pos.get("shares")) or 0
    pct = _f(pos.get("pct"))
    amp = _f(pos.get("amplitude"))
    if amp is None and prev_close:
        amp = (high - low) / prev_close * 100
    rebound = _pct(price, low) or 0.0
    rng = high - low
    pos_in_range = (price - low) / rng if rng > 0 else 0.5
    low20, low60, ma60 = _f(tech.get("low20")), _f(tech.get("low60")), _f(tech.get("ma60"))
    made_low20 = bool(low20 and low <= low20 * 1.0001)
    limit_down, limit_up = _f(pos.get("limit_down")), _f(pos.get("limit_up"))
    out = []

    metrics = {"pct": pct, "amplitude": round(amp, 2) if amp is not None else None,
               "rebound_from_low_pct": round(rebound, 2), "pos_in_range": round(pos_in_range, 2),
               "high": high, "low": low, "low20": low20, "made_low20": made_low20,
               "available": avail, "roles": sorted(roles), "sleeve": sleeve}

    def cand(t, rule, zone, qty, reason, flags=None, extra=None):
        z = [_rp(min(zone), price), _rp(max(zone), price)] if zone else None
        m = dict(metrics)
        m.update(extra or {})
        return {"type": t, "rule": rule, "zone": z, "qty": qty, "reason": reason,
                "flags": flags or [], "metrics": m}

    # ---- 止损 ----
    Ps = params["stop"]
    sell_all = round_lot(avail * Ps["sell_frac"], lot) or int(avail)
    guard = []
    if limit_down and price <= limit_down * (1 + Ps["limit_guard_pct"] / 100):
        guard = ["贴近跌停"]
    for lvl in cfg.get("stop_levels", []):
        if price < lvl:
            out.append(cand("stop", "stop_level", [price, lvl], sell_all,
                            f"跌破防守线 {lvl}（现价 {price}）", guard))
    if "reduce" in roles and made_low20 and _pct(price, low) <= Ps["near_low_pct"]:
        out.append(cand("stop", "stop_new_low20", [low, price], sell_all,
                        f"今天创20日新低 {low}，现价 {price} 距低点仅 {rebound:.1f}%", guard))
    for lvl in cfg.get("warn_levels", []):
        if price < lvl and not any(price < s for s in cfg.get("stop_levels", [])):
            out.append(cand("alert", "warn_level", None, None, f"跌破警戒线 {lvl}（现价 {price}）"))

    # ---- 反弹减仓 ----
    Pr = params["reduce"]
    if "reduce" in roles and rebound >= Pr["rebound_pct"]:
        names = {"ma5": "MA5", "ma10": "MA10", "ma20": "MA20", "prev_high": "昨高"}
        res = [(names[k], _f(tech.get(k))) for k in names]
        res.append(("昨收", prev_close))
        res = [(n, v) for n, v in res if v and v >= price * (1 - Pr["resist_near_pct"] / 100)]
        if res:
            n, r = min(res, key=lambda x: x[1])
            dist = _pct(r, price)
            if dist <= Pr["resist_near_pct"]:
                q = round_lot(avail * Pr["sell_frac"], lot) or (lot if avail >= lot else int(avail))
                out.append(cand("reduce", "reduce_rebound_resist", [price, r], q,
                                f"从日内低点 {low} 回升 {rebound:.1f}%，"
                                + (f"逼近压力位 {n} {r}（差 {dist:.1f}%）" if dist >= 0
                                   else f"刚站上压力位 {n} {r}（高出 {-dist:.1f}%）"),
                                extra={"resist": r, "resist_name": n}))

    # ---- 做T ----
    Pt = params["t"]
    if "t" in roles and amp is not None and amp >= Pt["min_amplitude_pct"]:
        tq = round_lot(shares * Pt["qty_frac"], lot) or lot
        # 做T必须成对：高抛带回补区、低吸带卖回区（同数量），否则就不是做T而是减仓/加仓
        # 交易仓：两腿价差至少 sleeves.trade.min_move_pct（3%），避免小波动来回做
        d = max(Pt.get("pair_min_pct", 1.5), amp * Pt.get("pair_amp_frac", 0.5),
                _f(trade_cfg(params).get("min_move_pct")) or 0) / 100
        w = Pt.get("pair_width_pct", 0.5) / 100
        if pos_in_range >= Pt["high_pos"]:
            q = min(tq, round_lot(avail, lot) or int(avail))
            if q > 0:
                c = cand("t_high", "t_high_range_top", [price, high], q,
                         f"振幅 {amp:.1f}%，现价处于日内区间 {pos_in_range:.0%} 位置，靠近日内高点 {high}")
                c["pair_zone"] = [_rp(price * (1 - d - w), price), _rp(price * (1 - d), price)]
                c["reason"] += f"；回补 {c['pair_zone'][0]}~{c['pair_zone'][1]} 接回 {q}"
                out.append(c)
        if rebound >= Pt["low_rebound_pct"] and pos_in_range <= Pt["low_pos"]:
            c = cand("t_low", "t_low_rebound", [low, price], tq,
                     f"振幅 {amp:.1f}%，从日内低点 {low} 回升 {rebound:.1f}%，仍在区间下部 {pos_in_range:.0%}")
            c["pair_zone"] = [_rp(price * (1 + d), price), _rp(price * (1 + d + w), price)]
            c["reason"] += f"；卖回 {c['pair_zone'][0]}~{c['pair_zone'][1]} 卖出 {tq}"
            out.append(c)

    # ---- 加仓 ----
    Pa = params["add"]
    if "add" in roles and rebound >= Pa["rebound_pct"]:
        why = []
        for n, s in (("20日低点", low20), ("60日低点", low60), ("MA60", ma60)):
            if s and (abs(_pct(price, s)) <= Pa["support_near_pct"] or low <= s <= price):
                why.append(f"{n} {s}")
        if pct is not None and pct <= Pa["drop_trigger_pct"]:
            why.append(f"当日跌 {pct}%")
        if made_low20:
            why.append("今天创20日新低")
        if why:
            amount = min((cash or 0) * Pa["cash_frac"], Pa["max_amount"])
            q = round_lot(amount / price, lot)
            c = cand("add", "add_rebound_support", [low, price], q,
                     f"从日内低点 {low} 回升 {rebound:.1f}%（止跌迹象），触发条件：{'、'.join(why)}；约 {q * price:.0f} 元",
                     extra={"score": round(rebound, 2)})
            prem = _f(pos.get("premium_pct"))
            pmax = cfg.get("premium_max", params["premium"]["default_max_pct"])
            if pos.get("is_etf") and prem is not None and prem > pmax:
                c["status"] = "blocked"
                c["reason"] += f"；但溢价 {prem}% > 上限 {pmax}%，屏蔽"
            if cfg.get("group"):
                c["group"] = cfg["group"]
            if c.get("status") != "blocked":
                apply_move_target(c, price, tech, params)
            out.append(c)

    # ---- 溢价 ----
    Pp = params["premium"]
    prem = _f(pos.get("premium_pct"))
    if pos.get("is_etf") and prem is not None and prem >= Pp["alert_pct"]:
        out.append(cand("premium", "premium_high", None, None,
                        f"溢价率 {prem}% ≥ {Pp['alert_pct']}%，不追、不定投", extra={"premium_pct": prem}))

    # ---- 异动 ----
    Pl = params["alert"]
    why = []
    if pct is not None and abs(pct) >= Pl["pct_abs"]:
        why.append(f"日内涨跌 {pct}%")
    if limit_down and price <= limit_down * (1 + Pl["limit_near_pct"] / 100):
        why.append(f"贴近跌停 {limit_down}")
    if limit_up and price >= limit_up * (1 - Pl["limit_near_pct"] / 100):
        why.append(f"贴近涨停 {limit_up}")
    if prev_pos and _f(prev_pos.get("price")):
        mv = _pct(price, _f(prev_pos["price"]))
        if mv is not None and abs(mv) >= Pl["move_15m_pct"]:
            why.append(f"较上一时点 {prev_pos['price']} 变化 {mv:+.1f}%")
    if why:
        out.append(cand("alert", "alert_move", None, None, "；".join(why)))

    for c in out:
        c.update({"code": pos.get("code"), "name": pos.get("position_name") or pos.get("name"),
                  "price": price, "pct": pct, "side": st.side_of(c["type"]), "label": st.label_of(c["type"]),
                  "quote_time": pos.get("quote_time")})
        c.setdefault("status", "new")
    return out


# ----------------------------------------------------------------- 自选股（只找买点）
def watch_qty(code, price, cash, P):
    """首仓数量：min(可用资金×cash_frac, max_amount) 按手取整；不足最小单位时给最小单位并标记。
    科创板(688)最小 200 股。返回 (qty, flags)。"""
    amount = min((cash or 0) * P["cash_frac"], P["max_amount"])
    min_q = 200 if str(code).startswith("688") else 100
    q = round_lot(amount / price, 100) if price else 0
    flags = []
    if q < min_q:
        q = min_q
        if q * price > amount:
            flags.append(f"最小单位 {min_q} 股约 {q * price:.0f} 元，超出建议金额 {amount:.0f} 元")
    return q, flags


def evaluate_watch(row, params, cash):
    """自选股（未持有）只出两类买点：👀自选低吸 watch_dip / 👀自选突破 watch_break。不做T、不减仓、不出异动。
    技术位（tech）来自前一交易日及以前的日 K，不含今天。"""
    P = params["watch"]
    price, low, high = _f(row.get("price")), _f(row.get("low")), _f(row.get("high"))
    if not price or not low or not high or row.get("is_today") is False or row.get("error"):
        return []
    tech = row.get("tech") or {}
    pct, vr = _f(row.get("pct")), _f(row.get("volume_ratio"))
    limit_up, limit_down = _f(row.get("limit_up")), _f(row.get("limit_down"))
    rebound = _pct(price, low) or 0.0
    rng = high - low
    pos_in_range = (price - low) / rng if rng > 0 else 0.5
    low20, high20 = _f(tech.get("low20")), _f(tech.get("high20"))
    made_low20 = bool(low20 and low < low20)
    metrics = {"pct": pct, "volume_ratio": vr, "rebound_from_low_pct": round(rebound, 2),
               "pos_in_range": round(pos_in_range, 2), "high": high, "low": low,
               "high20": high20, "low20": low20, "made_low20": made_low20,
               "ma20": _f(tech.get("ma20")), "ma60": _f(tech.get("ma60")), "watch": True}
    out = []

    def cand(t, rule, zone, reason, score, extra=None):
        q, flags = watch_qty(row.get("code"), price, cash, P)
        m = dict(metrics, score=round(score, 2), **(extra or {}))
        return {"type": t, "rule": rule, "zone": [_rp(min(zone), price), _rp(max(zone), price)], "qty": q,
                "reason": reason + f"；首仓 {q} 股约 {q * price:.0f} 元", "flags": flags, "metrics": m}

    # ---- 低吸：从日内低点回升，且靠近/触及支撑 ----
    if (rebound >= P["dip_rebound_pct"] and (pct is None or P["dip_min_pct"] <= pct <= P.get("dip_max_pct", 3.0))
            and not (limit_down and low <= limit_down * 1.001)):
        why = []
        for n, k in (("MA20", "ma20"), ("MA60", "ma60"), ("前20日低", "low20"), ("前60日低", "low60")):
            v = _f(tech.get(k))
            if v and (abs(_pct(price, v)) <= P["dip_support_near_pct"] or low <= v <= price):
                why.append(f"{n} {v:g}")
        if why:
            out.append(cand("watch_dip", "watch_dip_support", [low, price],
                            f"从日内低点 {low} 回升 {rebound:.1f}%，靠近/触及支撑：{'、'.join(why)}"
                            + ("（今天跌破前20日低后收回）" if made_low20 else ""),
                            rebound, {"supports": why}))

    # ---- 突破：放量站上前 20 日高 ----
    if (high20 and price > high20 and pct is not None and P["break_min_pct"] <= pct <= P["break_max_pct"]
            and vr is not None and vr >= P["break_vol_ratio"] and pos_in_range >= P["break_pos"]
            and not (limit_up and price >= limit_up * 0.995)):
        out.append(cand("watch_break", "watch_break_high20", [high20, price],
                        f"放量（量比 {vr:g}）站上前20日高 {high20:g}，涨 {pct}%，处于日内区间 {pos_in_range:.0%}",
                        vr, {"break_level": high20}))

    for c in out:
        apply_move_target(c, price, tech, params)
        c.update({"code": row.get("code"), "name": row.get("name"), "price": price, "pct": pct,
                  "side": st.side_of(c["type"]), "label": st.label_of(c["type"]),
                  "quote_time": row.get("quote_time"), "watch": True, "focus": row.get("focus")})
        c.setdefault("status", "new")
    return out


def classify_pulse(snap, params):
    """根据快照 market_pulse（量能 + 盘前份额 shares）/ policy_news 判定大盘脉搏等级。不产生信号。
    量能与份额各自给等级，取较高者；份额只在盘前快照里有（上一交易日净申购）。"""
    P = params.get("market_pulse") or {}
    mp = snap.get("market_pulse") or {}
    news = snap.get("policy_news") or []
    res = {"level": "none", "label": "无数据", "reasons": [], "hs300_ratio": None,
           "policy_news": [f"{n.get('time', '')[11:16]} {n.get('text', '')[:60]}" for n in news[:5]]}
    if not P.get("enabled", True) or not mp:
        return res
    rank = {"none": 0, "normal": 1, "watch": 2, "strong": 3}
    vol_level, vol_label = "none", "无数据"
    g = mp.get("groups") or {}
    hs = (g.get("hs300") or {}).get("median_ratio")
    if hs is not None:
        res["hs300_ratio"] = hs
        res["group_ratios"] = {k: v.get("median_ratio") for k, v in g.items()}
        idx = {i.get("name"): i for i in mp.get("indices") or [] if isinstance(i, dict)}
        hs_idx = idx.get("沪深300") or {}
        rebound = _f(hs_idx.get("from_low")) or 0.0
        rets = [_f(e.get("ret_15m")) for e in mp.get("etfs") or [] if e.get("group") == "hs300" and _f(e.get("ret_15m")) is not None]
        ret15 = sorted(rets)[len(rets) // 2] if rets else None
        res.update({"hs300_index_pct": hs_idx.get("pct"), "hs300_from_low": rebound, "hs300_ret_15m": ret15,
                    "as_of": mp.get("as_of")})
        small = (g.get("small") or {}).get("median_ratio")
        if hs >= P.get("strong_ratio", 3.0):
            vol_level, vol_label = "strong", "疑似国家队入场（沪深300ETF 显著放量）"
            res["reasons"].append(f"沪深300ETF 量比中位 {hs} ≥ {P.get('strong_ratio', 3.0)}")
        elif hs >= P.get("watch_ratio", 2.0) and rebound >= P.get("watch_rebound_pct", 1.0):
            vol_level, vol_label = "watch", "权重放量拉升，疑似护盘"
            res["reasons"].append(f"沪深300ETF 量比中位 {hs}，沪深300 从日内低点回升 {rebound}%")
        elif ret15 is not None and hs >= P.get("spike_vol_ratio", 1.5) and ret15 >= P.get("spike_ret_15m", 0.8):
            vol_level, vol_label = "watch", "权重 15 分钟急拉，疑似护盘"
            res["reasons"].append(f"沪深300ETF 量比中位 {hs}，近 15 分钟中位 {ret15:+.2f}%")
        else:
            vol_level, vol_label = "normal", "未见国家队迹象"
            res["reasons"].append(f"沪深300ETF 量比中位 {hs}" + (f"，中小盘 {small}" if small is not None else ""))

    sh_level, sh_label = "none", None
    sh = mp.get("shares") or {}
    shg = (sh.get("groups") or {}).get("hs300") or {}
    if shg.get("chg_pct") is not None:
        d1 = shg["chg_pct"]
        hist = [h.get("groups", {}).get("hs300") for h in sh.get("history") or []]
        hist = [x for x in hist if x is not None]
        cum = None
        if hist:
            c = 1.0
            for x in hist:
                c *= 1 + x / 100
            cum = round((c - 1) * 100, 2)
        res.update({"shares_date": sh.get("date"), "shares_hs300_pct": d1, "shares_hs300_yi": shg.get("chg_yi"),
                    "shares_hs300_cum_pct": cum, "shares_days": len(hist),
                    "shares_groups": {k: v.get("chg_pct") for k, v in (sh.get("groups") or {}).items()}})
        txt = f"{sh.get('date')} 沪市沪深300ETF 份额 {d1:+.2f}%（{shg.get('chg_yi', 0):+.1f} 亿份）"
        if cum is not None and len(hist) > 1:
            txt += f"，近 {len(hist)} 日累计 {cum:+.2f}%"
        if d1 >= P.get("shares_strong_pct", 4.5):
            sh_level, sh_label = "strong", "疑似国家队申购（沪深300ETF 份额大增）"
        elif d1 >= P.get("shares_watch_pct", 2.5) or (cum is not None and len(hist) > 1 and cum >= P.get("shares_watch_cum_pct", 8.0)):
            sh_level, sh_label = "watch", "沪深300ETF 份额持续净申购，疑似护盘资金"
        else:
            sh_level = "normal"
        res["reasons"].append(txt)

    if rank[sh_level] > rank[vol_level]:
        res["level"], res["label"] = sh_level, sh_label or "未见国家队迹象"
    else:
        res["level"], res["label"] = vol_level, vol_label
    if news:
        res["reasons"].append(f"政策快讯 {len(news)} 条")
    return res


def engine_ctx(snap, params, auto):
    """EV / 仓位计算用的资金上下文（calibrate.py ev 也用）。"""
    acc = snap.get("account") or {}
    pos_value, lot, min_lot = {}, {}, {}
    for p in snap.get("positions") or []:
        c = p.get("code")
        pos_value[c] = _f(p.get("market_value")) or (_f(p.get("price")) or 0) * (_f(p.get("shares")) or 0)
        lot[c] = lot_of(p, params, code_cfg(p, params))
    for r in snap.get("watchlist") or []:
        c = r.get("code")
        lot.setdefault(c, 100)
        min_lot[c] = 200 if str(c).startswith("688") else 100
    pf = portfolio_info(snap, params)
    return {"auto": auto, "regime": ap.regime_of(auto), "params": params,
            "total_assets": _f(acc.get("total_assets")), "cash": _f(acc.get("cash_available")),
            "pos_value": pos_value, "lot": lot, "min_lot": min_lot,
            "n_positions": pf["n_positions"], "held": set(pf["held"]), "dust": pf["dust"],
            "sleeves": pf["sleeves"], "trade_value": pf["trade_value"], "trade_budget": pf["trade_budget"]}


def sleeves_of(snap, params, core="load"):
    """估值过期保留原归属；缺失时保留最后确认归属或明确的长线定位。"""
    if core == "load":
        core = cv.load_core(params, day_of(snap))
    out = {}
    sc = params.get("sleeves") or {}
    force_trade = (sc.get("trade") or {}).get("force") or []
    for p in snap.get("positions") or []:
        code = str(p.get("code"))
        sl = cv.sleeve_of(code, params, core)
        if (core or {}).get("_health", {}).get("status") == "missing" and code not in (core.get("sleeves") or {}) \
                and code not in force_trade and "长线仓" in (p.get("stance") or ""):
            sl = "core"
        out[code] = sl
    return out


def portfolio_info(snap, params, core="load"):
    """两仓：长线仓只数上限 max_positions、单只目标 ≥ min_position_yuan、< dust_yuan 算碎仓（只看长线仓）；
    交易仓不限只数，市值上限 = 总资产 × sleeves.trade.budget_frac，超出时只卖不买。"""
    sz = (params.get("auto") or {}).get("sizing") or {}
    TA = _f((snap.get("account") or {}).get("total_assets")) or 0
    maxp = sz.get("max_positions")
    min_pos, dust_y = sz.get("min_position_yuan", 30000), sz.get("dust_yuan", 10000)
    sl = sleeves_of(snap, params, core)
    rows = []
    for p in snap.get("positions") or []:
        v = _f(p.get("market_value")) or (_f(p.get("price")) or 0) * (_f(p.get("shares")) or 0)
        if v > 0 or (_f(p.get("shares")) or 0) > 0:
            rows.append({"code": p.get("code"), "name": p.get("position_name") or p.get("name"),
                         "value": round(v or 0, 0), "pct": round(v / TA * 100, 1) if TA else None,
                         "sleeve": sl.get(str(p.get("code")), "trade")})
    rows.sort(key=lambda r: r["value"])
    core_rows = [r for r in rows if r["sleeve"] == "core"]
    trade_rows = [r for r in rows if r["sleeve"] != "core"]
    n = len(core_rows)
    dust = [r for r in core_rows if r["value"] < dust_y]
    small = [r for r in core_rows if dust_y <= r["value"] < min_pos]
    tb_frac = trade_cfg(params).get("budget_frac", 0.2)
    trade_value = sum(r["value"] for r in trade_rows)
    trade_budget = tb_frac * TA if TA else None
    trade_room = (trade_budget - trade_value) if trade_budget is not None else None
    if maxp and n > maxp:
        note = f"长线仓 {n} 只，超过上限 {maxp} 只：不开新仓，碎仓出减仓信号时一次清掉"
    elif maxp and n == maxp:
        note = f"长线仓 {n} 只，已到上限 {maxp} 只：不开新仓"
    else:
        note = f"长线仓 {n} 只，还可开 {(maxp or n) - n} 只"
    if dust:
        note += f"；长线仓碎仓 {len(dust)} 只合计 {sum(r['value'] for r in dust):.0f} 元"
    if trade_budget is not None:
        note += (f"；交易仓 {len(trade_rows)} 只市值 {trade_value:.0f} 元 / 上限 {trade_budget:.0f} 元（总资产 {tb_frac:.0%}）"
                 + (f"，超出 {-trade_room:.0f} 元：交易仓只卖不买（做T 不受影响）" if trade_room < 0 else
                    f"，还可买 {trade_room:.0f} 元"))
    return {"n_positions": n, "max_positions": maxp, "slots_left": (maxp - n) if maxp else None,
            "min_position_yuan": min_pos, "dust_yuan": dust_y,
            "n_trade": len(trade_rows), "trade_value": round(trade_value, 0),
            "trade_budget": round(trade_budget, 0) if trade_budget is not None else None,
            "trade_room": round(trade_room, 0) if trade_room is not None else None,
            "sleeves": sl, "rows": rows,
            "held": [r["code"] for r in rows], "dust": dust, "small": small, "note": note}


def open_trades(snap, params, lookback=15):
    """交易仓里还在持有期内的买入（加仓/自选低吸/突破，已采纳且带 tp_price）：
    现价 ≥ 止盈价 → action=take_profit；持有满 hold_days 个交易日 → action=expire（收盘前按市价卖出这笔）。
    交易日按 data/report/signals 里有记录的日期计（每个交易日盘中任务都会写），只作提醒，不产生候选。"""
    day = day_of(snap)
    hold = trade_cfg(params).get("hold_days", 5)
    try:
        days = sorted(f[:8] for f in os.listdir(st.SIG_DIR) if re.fullmatch(r"\d{8}\.jsonl", f) and f[:8] <= day)
    except OSError:
        return []
    if day not in days:
        days.append(day)
    px = {str(p.get("code")): p for p in snap.get("positions") or []}
    out = []
    for d in days[-lookback:]:
        for sig in st.load_signals(d).values():
            if (sig.get("type") not in ("add", "watch_dip", "watch_break") or not sig.get("tp_price")
                    or sig.get("decision") != "accept" or sig.get("send_failed")):
                continue
            code = str(sig.get("code"))
            pos = px.get(code)
            if not pos:
                continue
            held_days = len([x for x in days if d < x <= day])
            price = _f(pos.get("price"))
            tp = _f(sig["tp_price"])
            act = ("take_profit" if price and tp and price >= tp else
                   "expire" if held_days >= (sig.get("hold_days") or hold) else "hold")
            out.append({"id": sig.get("id"), "code": code, "name": sig.get("name"), "type": sig.get("type"),
                        "date": d, "buy_price": sig.get("price"), "qty": sig.get("qty"), "tp_price": tp,
                        "price": price, "held_days": held_days, "hold_days": sig.get("hold_days") or hold,
                        "action": act,
                        "note": {"take_profit": f"已到止盈价 {tp}，卖出这笔 {sig.get('qty')}",
                                 "expire": f"已持有 {held_days} 个交易日，到期：收盘前卖出这笔 {sig.get('qty')}",
                                 "hold": f"持有第 {held_days} 天，止盈 {tp}"}[act]})
    return out


def auto_info(auto, params):
    if not auto:
        return {"enabled": False, "note": "rules_auto.json 缺失/过期/未启用，使用固定阈值，不算 EV"}
    reg = ap.regime_of(auto)
    return {"enabled": True, "calibrated_at": auto.get("generated_at"), "regime": reg,
            "regime_name": ap.REGIME_NAME.get(reg), "regime_detail": (auto.get("regime") or {}).get("detail"),
            "ks": ap.ks_for(auto, reg), "sizing": (params.get("auto") or {}).get("sizing")}


def run(snap, params, record=True, source="engine"):
    day, slot = day_of(snap), slot_of(snap)
    cash = _f((snap.get("account") or {}).get("cash_available"))
    prev = prev_snapshot(day, slot)
    prev_map = {p.get("code"): p for p in (prev or {}).get("positions") or []}
    auto = ap.load_auto(params, day)
    ks = ap.ks_for(auto, ap.regime_of(auto)) if auto else {}

    def p_for(code):
        return ap.effective(params, ks, ap.atr_of(auto, code)) if auto else params

    sl = sleeves_of(snap, params)
    core_status = cv.status(params, snap, day)
    risk_rows = {r["code"]: r for r in core_status.get("items") or [] if r.get("sleeve") == "core"}
    raw = []
    for pos in snap.get("positions") or []:
        code = str(pos.get("code"))
        rr = risk_rows.get(code) or {}
        risk = rr.get("risk") or {}
        found = evaluate_position(pos, p_for(code), cash, prev_map.get(pos.get("code")), sl.get(code, "trade"))
        if risk.get("state") in ("reduce", "exit"):
            found = [c for c in found if c["type"] not in ("t_high", "t_low", "add")]
            if rr.get("qty") and _f(pos.get("price")) and pos.get("is_today") is not False:
                typ = "stop" if risk["state"] == "exit" else "reduce"
                found.append({"code": code, "name": pos.get("position_name") or pos.get("name"),
                              "type": typ, "label": st.label_of(typ), "side": -1, "rule": "core_logic_exit",
                              "price": pos["price"], "zone": [pos["price"], pos["price"]], "qty": rr["qty"],
                              "status": "new", "reason": rr["action"], "flags": ["逻辑风险优先，发送不等于成交"],
                              "metrics": {"available": pos.get("available"), "sleeve": "core"},
                              "risk_exit": True, "cycle_id": risk.get("cycle_id")})
        elif rr.get("data_block"):
            for c in found:
                if c.get("side", 0) > 0:
                    c["status"] = "blocked"
                    c["reason"] += "；" + rr["data_block"]
        raw.extend(found)
    held = {p.get("code") for p in snap.get("positions") or []}
    if params.get("watch", {}).get("enabled", True):
        for row in snap.get("watchlist") or []:
            if row.get("code") not in held:
                raw.extend(evaluate_watch(row, p_for(row.get("code")), cash))

    # 同标的同方向只保留优先级最高的一条
    best = {}
    for c in raw:
        k = (c["code"], c["side"])
        if k not in best or (100 * bool(c.get("risk_exit")) + st.PRIORITY.get(c["type"], 0)) > (100 * bool(best[k].get("risk_exit")) + st.PRIORITY.get(best[k]["type"], 0)):
            best[k] = c
    cands = list(best.values())

    pulse = classify_pulse(snap, params)
    if pulse["level"] in ("strong", "watch"):
        sell_types = set((params.get("market_pulse") or {}).get("sell_types", ["stop", "reduce", "t_high"]))
        for c in cands:
            if c["type"] in sell_types and not c.get("risk_exit"):
                c["flags"] = list(c.get("flags") or []) + [f"大盘：{pulse['label']}，勿追跌、优先等反抽"]

    # 宽度闸门：市场宽度 ice/weak 时，只给买类候选加提醒 flag（不改信号、不改阈值）
    _reg = _market_regime(snap, params)
    if _reg and _reg.get("gate") and _reg.get("regime") in ("ice", "weak"):
        buy_types = set((params.get("market_regime") or {}).get(
            "buy_types", ["add", "t_low", "watch_dip", "watch_break"]))
        for c in cands:
            if c["type"] in buy_types:
                c["flags"] = list(c.get("flags") or []) + [f"大盘：{_reg['label']}，{_reg['gate']}"]

    # 二选一分组：只保留回升幅度最大的一只
    groups = {}
    for c in cands:
        if c["type"] == "add" and c.get("group") and c["status"] == "new":
            groups.setdefault(c["group"], []).append(c)
    for g, lst in groups.items():
        lst.sort(key=lambda c: c["metrics"].get("score", 0), reverse=True)
        for c in lst[1:]:
            c["status"] = "group_suppressed"
            c["reason"] += f"；与 {lst[0]['code']} 二选一，后者止跌更明显"

    # EV 与仓位：按优先级依次占用现金/仓位空间（同一时点多条买入不会合计突破现金底线）
    if auto:
        ctx = engine_ctx(snap, params, auto)
        for c in sorted(cands, key=lambda c: -st.PRIORITY.get(c["type"], 0)):
            if c["status"] != "new":
                continue
            if not c.get("risk_exit"):
                ap.size_and_ev(c, ctx)
            if c["status"] == "new" and c["side"] > 0 and c["type"] != "t_low" and c.get("qty"):
                amt = c["qty"] * c["price"]
                if c["code"] not in ctx["held"]:
                    ctx["held"].add(c["code"])
                if ctx["sleeves"].get(str(c["code"]), "trade") != "core":
                    ctx["trade_value"] = (ctx.get("trade_value") or 0) + amt
                ctx["cash"] = (ctx["cash"] or 0) - amt
                ctx["pos_value"][c["code"]] = ctx["pos_value"].get(c["code"], 0) + amt

    sigs = st.load_signals(day)
    lim = params["dedup"]["price_change_pct"]
    for c in cands:
        c["id"] = f"{day}-{slot.replace(':', '')}-{c['code']}-{c['type']}"
        c.update({"date": day, "time": slot, "source": source})
        if c.get("risk_exit"):
            d = next((sid for sid, s in sigs.items() if s.get("cycle_id") == c.get("cycle_id")
                      and not s.get("send_failed")), None)
        else:
            d = st.find_dup(sigs, c["code"], c["side"], c["price"], slot, lim,
                            include_all=c["status"] != "new")
        if d and d != c["id"]:
            c["status"], c["dup_of"] = "dup", d

    # 自选股每个时点最多 max_per_slot 条（低吸优先，其次按分数），其余标 capped、不记录
    cap = params.get("watch", {}).get("max_per_slot", 3)
    wl = sorted([c for c in cands if c.get("watch") and c["status"] == "new"],
                key=lambda c: (-st.PRIORITY.get(c["type"], 0), -c["metrics"].get("score", 0)))
    for c in wl[cap:]:
        c["status"] = "capped"
        c["reason"] += f"；本时点自选股已满 {cap} 条，未提交"

    if record:
        new_events = []
        for c in cands:
            if c["status"] in ("dup", "capped") or c["id"] in sigs:
                continue
            new_events.append({"event": "candidate", **c,
                               "snapshot_time": (snap.get("market") or {}).get("now")})
        if new_events:
            for c in new_events:
                if c.get("risk_exit"):
                    rr = risk_rows[c["code"]]
                    cv.rc.register_plan(rr["risk"], rr)
            st.append_events(day, new_events)

    order = lambda c: (-abs(c["side"]), c["code"])
    return {
        "date": day, "slot": slot, "snapshot_time": (snap.get("market") or {}).get("now"),
        "source": source, "recorded": record,
        "market_pulse": pulse,
        "market_regime": _reg,
        "portfolio_risk": core_status.get("portfolio_risk"),
        "core_risk": {"health": core_status.get("health"), "safe_mode": core_status.get("safe_mode"),
                      "items": [{"code": r["code"], "name": r.get("name"), "risk": r.get("risk"),
                                 "action": r.get("action"), "qty": r.get("qty"), "data_block": r.get("data_block")}
                                for r in risk_rows.values()]},
        "auto": auto_info(auto, params),
        "portfolio": {k: v for k, v in portfolio_info(snap, params).items() if k not in ("held", "rows")},
        "candidates": sorted([c for c in cands if c["status"] == "new"], key=order),
        "suppressed": [c for c in cands if c["status"] in ("blocked", "group_suppressed", "capped", "ev_negative",
                                                           "slots_full")],
        "open_trades": open_trades(snap, params),
        "duplicates": [{"code": c["code"], "name": c["name"], "type": c["type"], "price": c["price"],
                        "dup_of": c.get("dup_of")} for c in cands if c["status"] == "dup"],
        "next_step": ("逐条判断 candidates：采纳 → python3 main.py record decide <id> accept --note 理由；"
                      "否决 → python3 main.py record decide <id> reject --note 理由；"
                      "发送通知成功后 → python3 main.py record sent <id>..."),
    }


def premarket_levels(snap, params):
    out = []
    auto = ap.load_auto(params, day_of(snap))
    ks = ap.ks_for(auto, ap.regime_of(auto)) if auto else {}
    sz = (params.get("auto") or {}).get("sizing") or {}
    acc = snap.get("account") or {}
    TA, cash = _f(acc.get("total_assets")), _f(acc.get("cash_available"))
    room_cash = (cash - sz.get("cash_floor", 0.2) * TA) if TA and cash is not None else None
    pf = portfolio_info(snap, params)
    sl, trade_room = pf["sleeves"], pf["trade_room"]
    for pos in snap.get("positions") or []:
        price = _f(pos.get("price"))
        tech = pos.get("tech") or {}
        if not price:
            continue
        lv = {"MA5": tech.get("ma5"), "MA10": tech.get("ma10"), "MA20": tech.get("ma20"),
              "MA60": tech.get("ma60"), "20日高": tech.get("high20"), "20日低": tech.get("low20"),
              "60日高": tech.get("high60"), "60日低": tech.get("low60"),
              "昨高": tech.get("prev_high"), "昨低": tech.get("prev_low")}
        lv = {k: _f(v) for k, v in lv.items() if _f(v)}
        cfg = code_cfg(pos, params)
        out.append({
            "code": pos.get("code"), "name": pos.get("position_name") or pos.get("name"),
            "sleeve": sl.get(str(pos.get("code")), "trade"),
            "price": price, "roles": (["t"] if sl.get(str(pos.get("code"))) == "core" else sorted(roles_of(pos, params))),
            "available": pos.get("available"),
            "supports": sorted([(k, v) for k, v in lv.items() if v < price], key=lambda x: -x[1])[:3],
            "resistances": sorted([(k, v) for k, v in lv.items() if v > price], key=lambda x: x[1])[:3],
            "stop_levels": cfg.get("stop_levels", []), "warn_levels": cfg.get("warn_levels", []),
            "premium_pct": pos.get("premium_pct"), "premium_max": cfg.get("premium_max"),
        })
        if auto:
            a = ap.atr_of(auto, pos.get("code"))
            p = ap.effective(params, ks, a)
            mv = _f(pos.get("market_value")) or 0
            room_pos = sz.get("position_cap", 0.2) * TA - mv if TA else None
            out[-1].update({
                "atr20_pct": a,
                "thresholds": {"做T最小振幅%": p["t"]["min_amplitude_pct"], "做T回补价差%": p["t"]["pair_min_pct"],
                               "加仓回升%": p["add"]["rebound_pct"], "加仓距支撑%": p["add"]["support_near_pct"],
                               "减仓回升%": p["reduce"]["rebound_pct"], "减仓距压力%": p["reduce"]["resist_near_pct"]},
                "position_pct": round(mv / TA * 100, 1) if TA else None,
                # 交易仓：盘中买入还受交易仓预算约束；长线仓的买入按 core_value.py 的分区与目标仓位，不走盘中规则
                "buy_room_yuan": (round(max(0.0, min(room_pos, room_cash, trade_room if trade_room is not None else room_cash)), 0)
                                  if room_pos is not None and room_cash is not None and out[-1]["sleeve"] != "core" else None),
            })
    watch = []
    for row in snap.get("watchlist") or []:
        tech = row.get("tech") or {}
        price = _f(row.get("price")) or _f(tech.get("close"))
        if not price:
            continue
        lv = {"MA5": tech.get("ma5"), "MA10": tech.get("ma10"), "MA20": tech.get("ma20"),
              "MA60": tech.get("ma60"), "前20日高": tech.get("high20"), "前20日低": tech.get("low20"),
              "前60日高": tech.get("high60"), "前60日低": tech.get("low60")}
        lv = {k: _f(v) for k, v in lv.items() if _f(v)}
        watch.append({
            "code": row.get("code"), "name": row.get("name"), "industry": row.get("industry"), "price": price,
            "chg5_pct": tech.get("chg5_pct"), "chg20_pct": tech.get("chg20_pct"),
            "supports": sorted([(k, v) for k, v in lv.items() if v < price], key=lambda x: -x[1])[:3],
            "resistances": sorted([(k, v) for k, v in lv.items() if v > price], key=lambda x: x[1])[:3],
            "announcements_new": [a.get("title") for a in row.get("announcements_new") or []],
            "focus": row.get("focus"),
        })
    return {"mode": "premarket", "levels": out, "watch_levels": watch,
            "market_pulse": classify_pulse(snap, params),
            "market_regime": _market_regime(snap, params),
            "auto": auto_info(auto, params),
            "portfolio": {k: v for k, v in pf.items() if k not in ("held", "rows")},
            "core": cv.status(params, snap, day_of(snap)),
            "open_trades": open_trades(snap, params),
            "cash_room_yuan": round(max(0.0, room_cash), 0) if room_cash is not None else None}


def main(argv):
    args = list(argv)
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0

    def opt(name, default=None):
        if name in args:
            i = args.index(name)
            v = args[i + 1] if i + 1 < len(args) else default
            del args[i:i + 2]
            return v
        return default

    mode = opt("--mode", "intraday")
    path = opt("--snapshot")
    source = opt("--source", "engine")
    record = "--no-record" not in args
    bad = [a for a in args if a != "--no-record"]
    if bad:
        print(f"未知参数 {bad}，用 --help 查看用法；本次未记录任何信号", file=sys.stderr)
        return 2
    params = st.load_params()
    path = path or (SNAP_PRE if mode == "premarket" else SNAP_LIVE)
    with open(path, encoding="utf-8") as f:
        snap = json.load(f)
    if mode == "premarket":
        res = premarket_levels(snap, params)
    else:
        if not (snap.get("market") or {}).get("a_open_today", True):
            res = {"date": day_of(snap), "closed": True, "candidates": []}
        else:
            res = run(snap, params, record=record, source=source)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
