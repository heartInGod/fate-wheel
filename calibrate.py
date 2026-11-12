#!/usr/bin/env python3
"""动态阈值校准：用近 40 个交易日的 5 分钟 K 回放规则引擎，按扣手续费后的收益校准阈值，写 rules_auto.json。

用法：
  python3 calibrate.py                 # 拉数据（增量缓存）→ 回放 → 校准 → 写 rules_auto.json 与 logs/calib/yyyyMMdd.md
  python3 calibrate.py --offline       # 只用 cache/calib 里已有的数据
  python3 calibrate.py --dry-run       # 只算不写
  python3 calibrate.py show            # 看当前生效的市场状态、k 值、各持仓的具体阈值与仓位空间
  python3 calibrate.py ev --code 159813 --type add --price 0.53 [--qty 10000]
                                       # 给大模型自行补充的信号算 EV（扣手续费）与建议数量

做法（详见 auto_params.py 文件头）：
  * 标的：holdings.csv 里的 A 股/ETF + watchlist.csv（港股没有历史分钟 K，不回测，借用 A 股个股统计）
  * 数据：新浪 5 分钟 K（约 41 个交易日，增量合并到 cache/calib/m5/）、腾讯不复权日 K、沪深300 日 K
  * 回放：每个交易日 09:40~14:50 每 10 分钟一个时点，只用截至该时点的 5 分钟 K 拼出与实盘相同字段的快照，
    直接调用 signals_engine.evaluate_position / evaluate_watch；同时点同方向取优先级最高，
    同标的同方向当天价格变化 < dedup.price_change_pct 视为重复（与实盘一致）
  * 收益：买入类（交易仓）= 次日起 hold_days（5）天内最高价碰到 +min_move_pct（3%）记 +3%，否则按第 5 日收盘/信号价−1，
    上方 20/60 日高点空间不足 3% 的候选被 blocked、不计；减仓/止损 = 取负；做T = 当天或 T+1 碰到回补/卖回价记成功（收益 d），
    否则按 T+1 收盘平仓；均为毛收益，目标函数按 ref_amount（1 万元）扣往返/单边手续费
  * 搜索：每组参数坐标搜索（每个参数在 bounds 内取 7 个点，2 轮），目标 = Σ净收益 − z×√n×标准差，
    样本 < min_samples 的取值不采用；市场状态（strong/range/weak，用沪深300 前一日收盘与 MA20 及其斜率划分）
    天数 ≥ regime_min_days 的单独再搜一次，按 天数/(天数+shrink_k) 向全体结果收缩
  * 生效：在前值 ±daily_change_pct% 与 bounds 内取最接近目标的值（首次以固定阈值折算的 k 为前值）
  * 统计：最终参数下的回测样本 + logs/eval 里规则信号的实盘结果，按 规则→品种→品种|市场状态 分层收缩，
    供引擎算 EV
"""
import datetime
import json
import math
import os
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

import auto_params as ap
import fees
import signal_store as st
import signals_engine as se
from datasources import quotes
from datasources._http import http_get

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache", "calib")
M5_DIR = os.path.join(CACHE, "m5")
REPORT_DIR = os.path.join(ROOT, "logs", "calib")
EVAL_DIR = os.path.join(ROOT, "logs", "eval")

SLOTS = ["09:40", "09:50", "10:00", "10:10", "10:20", "10:30", "10:40", "10:50", "11:00", "11:10", "11:20", "11:30",
         "13:00", "13:10", "13:20", "13:30", "13:40", "13:50", "14:00", "14:10", "14:20", "14:30", "14:40", "14:50"]
STOP_RULES = ["stop_new_low20"]
BUY_RULES = {"add_rebound_support", "watch_dip_support", "watch_break_high20"}   # 交易仓买入类：+3% 止盈 / 持有 5 日
HOLD_MAX = 10   # build_states 预存的未来日 K 天数（≥ sleeves.trade.hold_days）
GRID_N = 7
ROUNDS = 2
DESC = {
    "t_amp_k": "做T最小振幅（×ATR）", "t_high_pos": "做T高抛：日内位置≥", "t_low_pos": "做T低吸：日内位置≤",
    "t_reb_k": "做T低吸：距低点回升（×ATR）", "t_pair_k": "做T回补/卖回最小价差（×ATR）",
    "add_reb_k": "加仓：距低点回升（×ATR）", "add_sup_k": "加仓：距支撑（×ATR）", "add_drop_k": "加仓：当日跌幅触发（×ATR）",
    "red_reb_k": "减仓：距低点回升（×ATR）", "red_res_k": "减仓：距压力位（×ATR）",
    "wd_reb_k": "自选低吸：距低点回升（×ATR）", "wd_sup_k": "自选低吸：距支撑（×ATR）",
    "wb_min_k": "自选突破：最小涨幅（×ATR）", "wb_vol": "自选突破：量比≥", "wb_pos": "自选突破：日内位置≥",
}
RULE_NAME = {"t_high_range_top": "做T高抛", "t_low_rebound": "做T低吸", "add_rebound_support": "加仓",
             "reduce_rebound_resist": "反弹减仓", "stop_new_low20": "止损(20日新低)",
             "watch_dip_support": "自选低吸", "watch_break_high20": "自选突破"}
TYPE_RULE = {"add": "add_rebound_support", "t_high": "t_high_range_top", "t_low": "t_low_rebound",
             "reduce": "reduce_rebound_resist", "stop": "stop_new_low20",
             "watch_dip": "watch_dip_support", "watch_break": "watch_break_high20"}


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _mins(hhmm):
    h, m = map(int, hhmm[:5].split(":"))
    return h * 60 + m


def elapsed(hhmm):
    """开盘后已交易的分钟数。"""
    t = _mins(hhmm)
    if t <= 11 * 60 + 30:
        return max(1, t - (9 * 60 + 30))
    return 120 + max(0, t - 13 * 60)


# ----------------------------------------------------------------- 数据
def universe():
    import csv
    codes = []
    for fn, col in (("holdings.csv", "代码"), ("watchlist.csv", "代码")):
        try:
            with open(os.path.join(ROOT, fn), encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    c = (r.get(col) or "").strip()
                    if re.fullmatch(r"\d{6}", c) and c not in codes:
                        codes.append(c)
        except OSError:
            pass
    return codes


def fetch_m5(code):
    """新浪 5 分钟 K（约 1970 根），与缓存按时间合并；只保留收完盘（有 15:00 这根）的交易日。"""
    os.makedirs(M5_DIR, exist_ok=True)
    path = os.path.join(M5_DIR, f"{code}.json")
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = {r["t"]: r for r in json.load(f)}
        except (OSError, json.JSONDecodeError, KeyError):
            old = {}
    sym = quotes.normalize(code)["tencent"]
    txt = http_get("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_x=/CN_MarketDataService.getKLineData",
                   params={"symbol": sym, "scale": "5", "ma": "no", "datalen": "1970"}, timeout=15)
    i, j = txt.find("("), txt.rfind(")")
    rows = json.loads(txt[i + 1:j]) if i >= 0 and j > i else []
    for r in rows or []:
        o, h, l, c, v = (_f(r.get(k)) for k in ("open", "high", "low", "close", "volume"))
        if c:
            old[r["day"]] = {"t": r["day"], "o": o, "h": h, "l": l, "c": c, "v": v or 0.0}
    days = {}
    for t in old:
        days.setdefault(t[:10], set()).add(t[11:16])
    done = {d for d, ts in days.items() if "15:00" in ts}
    out = sorted((r for t, r in old.items() if t[:10] in done), key=lambda r: r["t"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


def load_m5(code):
    path = os.path.join(M5_DIR, f"{code}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_daily(code, n=200):
    k = quotes.kline(code, "day", n, "")
    path = os.path.join(CACHE, "day", f"{code}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(k, f, ensure_ascii=False)
    return k


def load_daily(code):
    with open(os.path.join(CACHE, "day", f"{code}.json"), encoding="utf-8") as f:
        return json.load(f)


def load_all(codes, offline=False):
    """返回 ({code: m5 rows}, {code: daily rows}, hs300 daily, errors)。"""
    m5, day, errs = {}, {}, []

    def one(c):
        try:
            if offline:
                return c, load_m5(c), load_daily(c), None
            return c, fetch_m5(c), fetch_daily(c), None
        except Exception as e:  # noqa: BLE001
            try:  # 拉取失败退回缓存
                return c, load_m5(c), load_daily(c), f"{c} 拉取失败，用缓存：{e}"
            except Exception:  # noqa: BLE001
                return c, None, None, f"{c} 无数据：{e}"

    with ThreadPoolExecutor(max_workers=6) as ex:
        for c, a, b, e in ex.map(one, codes):
            if e:
                errs.append(e)
            if a and b:
                m5[c], day[c] = a, b
    hs = load_daily("sh000300") if offline else None
    if hs is None:
        try:
            hs = fetch_daily("sh000300", 260)
        except Exception as e:  # noqa: BLE001
            errs.append(f"沪深300 日K：{e}")
            hs = load_daily("sh000300")
    return m5, day, hs, errs


# ----------------------------------------------------------------- 指标
def atr_series(daily):
    """{date: 截至该日（含）的 ATR20%}"""
    out, trs = {}, []
    for i, r in enumerate(daily):
        if i == 0 or not r["close"]:
            continue
        pc = daily[i - 1]["close"]
        if not pc:
            continue
        tr = max(r["high"] - r["low"], abs(r["high"] - pc), abs(r["low"] - pc)) / pc * 100
        trs.append(tr)
        if len(trs) >= 20:
            out[r["date"]] = sum(trs[-20:]) / 20
    return out


def regime_series(hs):
    """{date: 该日收盘后判定的市场状态}；某交易日 d 使用的是 d 前一日的判定。"""
    closes = [r["close"] for r in hs]
    out = {}
    for i, r in enumerate(hs):
        if i < 25:
            continue
        ma = sum(closes[i - 19:i + 1]) / 20
        ma5ago = sum(closes[i - 24:i - 4]) / 20
        c, slope = closes[i], ma - ma5ago
        reg = "strong" if c > ma and slope > 0 else "weak" if c < ma and slope < 0 else "range"
        out[r["date"]] = {"regime": reg, "close": c, "ma20": round(ma, 2), "ma20_slope5": round(slope, 2)}
    return out


# ----------------------------------------------------------------- 回放用快照
def limit_pct(code):
    return 0.2 if code.startswith(("300", "301", "688")) else 0.1


def build_states(code, m5, daily, atr, regimes):
    """预计算每个 (交易日, 时点) 的持仓式 / 自选式快照字段，返回 list[day dict]。"""
    by_day = {}
    for r in m5:
        by_day.setdefault(r["t"][:10], []).append(r)
    dates = [r["date"] for r in daily]
    didx = {d: i for i, d in enumerate(dates)}
    mdays = sorted(by_day)
    vol_tot = {d: sum(b["v"] for b in by_day[d]) for d in mdays}
    nd = 3 if (daily[-1]["close"] or 0) < 10 else 2
    out = []
    for n, d in enumerate(mdays):
        i = didx.get(d)
        if i is None or i < 21:
            continue
        prev = daily[i - 1]
        pc = prev["close"]
        a = atr.get(prev["date"])
        reg = (regimes.get(prev["date"]) or {}).get("regime")
        if not pc or not a or not reg:
            continue
        hist = daily[max(0, i - 120):i]           # 截至前一交易日
        nxt = daily[i + 1] if i + 1 < len(daily) else None
        nxt_bars = by_day.get(nxt["date"]) if nxt else None
        prev5 = [vol_tot[x] for x in mdays[max(0, n - 5):n]]
        avg5 = sum(prev5) / len(prev5) if len(prev5) == 5 else None
        lu, ld = round(pc * (1 + limit_pct(code)), nd), round(pc * (1 - limit_pct(code)), nd)
        hc = [r["close"] for r in hist]

        def ma(xs, k):
            return sum(xs[-k:]) / k if len(xs) >= k else None

        w_tech = {"ma5": ma(hc, 5), "ma10": ma(hc, 10), "ma20": ma(hc, 20), "ma60": ma(hc, 60),
                  "high20": max(r["high"] for r in hist[-20:]), "low20": min(r["low"] for r in hist[-20:]),
                  "high60": max(r["high"] for r in hist[-60:]), "low60": min(r["low"] for r in hist[-60:]),
                  "prev_high": prev["high"], "prev_low": prev["low"]}
        bars = by_day[d]
        slots = []
        hi = lo = None
        cum_v = 0.0
        k = 0
        for s in SLOTS:
            while k < len(bars) and bars[k]["t"][11:16] <= s:
                b = bars[k]
                hi = b["h"] if hi is None else max(hi, b["h"])
                lo = b["l"] if lo is None else min(lo, b["l"])
                cum_v += b["v"]
                k += 1
            if hi is None:
                continue
            price = bars[k - 1]["c"]
            pct = round((price / pc - 1) * 100, 2)
            cl = hc + [price]
            p_tech = {"ma5": ma(cl, 5), "ma10": ma(cl, 10), "ma20": ma(cl, 20), "ma60": ma(cl, 60),
                      "high20": max([r["high"] for r in hist[-19:]] + [hi]),
                      "low20": min([r["low"] for r in hist[-19:]] + [lo]),
                      "low60": min([r["low"] for r in hist[-59:]] + [lo]),
                      "high60": max([r["high"] for r in hist[-59:]] + [hi]),
                      "prev_high": prev["high"], "prev_low": prev["low"]}
            vr = round(cum_v / elapsed(s) / (avg5 / 240), 2) if avg5 else None
            base = {"code": code, "price": price, "high": hi, "low": lo, "prev_close": pc, "pct": pct,
                    "amplitude": round((hi - lo) / pc * 100, 2), "limit_up": lu, "limit_down": ld,
                    "is_today": True, "volume_ratio": vr}
            pos = dict(base, tech=p_tech, shares=10000, available=10000, stance="", market="a",
                       name=code, position_name=code)
            row = dict(base, tech=w_tech, name=code)
            slots.append({"slot": s, "pos": pos, "watch": row, "k": k,
                          "rebound": (price / lo - 1) * 100 if lo else 0.0})
        out.append({"date": d, "atr": a, "regime": reg, "slots": slots, "bars": bars,
                    "next_bars": nxt_bars, "next_close": nxt["close"] if nxt else None,
                    "fut_days": daily[i + 1:i + 1 + HOLD_MAX]})
    return out


# ----------------------------------------------------------------- 回放
def prefilter(fam, s, atr, lo):
    """与参数无关的必要条件（按 bounds 下限），先剔掉不可能触发的时点，回放提速。"""
    if fam == "t":
        return s["pos"]["amplitude"] >= lo["t_amp_k"] * atr
    if fam == "add":
        return s["rebound"] >= lo["add_reb_k"] * atr
    if fam == "reduce":
        return s["rebound"] >= lo["red_reb_k"] * atr
    if fam == "watch_dip":
        return s["rebound"] >= lo["wd_reb_k"] * atr
    if fam == "watch_break":
        w = s["watch"]
        return w["price"] > w["tech"]["high20"] and w["pct"] >= lo["wb_min_k"] * atr
    if fam == "stop":
        p = s["pos"]
        return p["low"] <= p["tech"]["low20"] * 1.0001
    return True


def outcome(c, day, slot_k, params_eff):
    """返回毛收益（%，按建议方向），数据不够时返回 None。
    做T：两腿价差 d（≥ 交易仓 min_move_pct）当日/次日到价记 +d，否则按 T+1 收盘平仓；
    买入类（交易仓）：次日起 hold_days 个交易日内最高价碰到 +min_move_pct 记 +min_move_pct，否则按第 hold_days 日收盘；
    减仓/止损：T+1 收盘。"""
    c1 = day["next_close"]
    if not c1 or not day["next_bars"]:
        return None
    p0 = c["price"]
    rule = c["rule"]
    tc = (params_eff.get("sleeves") or {}).get("trade") or {}
    mm = tc.get("min_move_pct") or 0
    if rule in BUY_RULES and mm > 0:
        hold = int(tc.get("hold_days", 5))
        fut = (day.get("fut_days") or [])[:hold]
        tp = p0 * (1 + mm / 100)
        for r in fut:   # 当天买入 T+1 才能卖，从次日开始看
            if r["high"] >= tp:
                return float(mm)
        if len(fut) < hold:
            return None
        return (fut[-1]["close"] / p0 - 1) * 100
    if rule in ("t_high_range_top", "t_low_rebound"):
        Pt = params_eff["t"]
        amp = c["metrics"].get("amplitude") or 0
        d = max(Pt.get("pair_min_pct", 1.5), amp * Pt.get("pair_amp_frac", 0.5), mm) / 100
        fut = day["bars"][slot_k:] + day["next_bars"]
        if rule == "t_high_range_top":
            if fut and min(b["l"] for b in fut) <= p0 * (1 - d):
                return d * 100
            return (p0 - c1) / p0 * 100
        if fut and max(b["h"] for b in fut) >= p0 * (1 + d):
            return d * 100
        return (c1 / p0 - 1) * 100
    r = (c1 / p0 - 1) * 100
    return -r if c["side"] < 0 else r


def replay(fam, ks, data, base, cfg, regime=None):
    """按 ks 回放一个规则组，返回样本 list[(rule, code, date, slot, gross%, net%, regime)]。"""
    spec = ap.FAMILIES.get(fam) or {"rules": STOP_RULES, "role": "reduce"}
    rules = set(spec["rules"]) | (set(STOP_RULES) if fam in ("reduce", "stop") else set())
    role = spec["role"]
    lo = {k: v[0] for k, v in cfg["bounds"].items()}
    lim = base["dedup"]["price_change_pct"]
    ref = cfg.get("ref_amount", 10000)
    watch = role == "watch"
    out = []
    for code, days in data.items():
        for day in days:
            if regime and day["regime"] != regime:
                continue
            k_use = ks(day["regime"]) if callable(ks) else ks
            p = None
            seen = []
            for s in day["slots"]:
                if not prefilter(fam, s, day["atr"], lo):
                    continue
                if p is None:
                    p = ap.effective(base, k_use, day["atr"])
                    p["codes"] = {code: {"roles": [role]}}
                cs = (se.evaluate_watch(s["watch"], p, 1e6) if watch
                      else se.evaluate_position(s["pos"], p, 1e6))
                best = {}
                for c in cs:
                    if c["rule"] not in rules and c["type"] not in ("stop",):
                        continue
                    if c["type"] == "stop" and c["rule"] not in rules:
                        continue
                    kk = c["side"]
                    if kk not in best or st.PRIORITY.get(c["type"], 0) > st.PRIORITY.get(best[kk]["type"], 0):
                        best[kk] = c
                for c in best.values():
                    if c.get("status") == "blocked":   # 例如上方空间不足 3%：实盘不会买，回放也不计
                        continue
                    if any(sd == c["side"] and abs(c["price"] / p0 - 1) * 100 < lim for sd, p0 in seen):
                        continue
                    seen.append((c["side"], c["price"]))
                    g = outcome(c, day, s["k"], p)
                    if g is None:
                        continue
                    fee = fees.cost_pct(ref, code, ap.RULE_LEGS.get(c["rule"], "round")) or 0
                    out.append((c["rule"], code, day["date"], s["slot"], g, g - fee, day["regime"]))
    return out


def objective(samples, rules, cfg):
    xs = [x[5] for x in samples if x[0] in rules]
    n = len(xs)
    if n < cfg.get("min_samples", 30):
        return -1e9 + n, n, None
    sd = statistics.pstdev(xs) if n > 1 else 0.0
    return sum(xs) - cfg.get("z", 1.0) * math.sqrt(n) * sd, n, sum(xs) / n


def grid(lo, hi, n=GRID_N):
    return [round(lo + (hi - lo) * i / (n - 1), 3) for i in range(n)]


def search(fam, start, data, base, cfg, regime=None, log=None):
    """坐标搜索；返回 (best_ks, (obj, n, mean_net))。"""
    spec = ap.FAMILIES[fam]
    rules = set(spec["rules"])
    cur = dict(start)
    memo = {}

    def score(ks):
        key = tuple(round(ks[p], 3) for p in spec["params"])
        if key not in memo:
            memo[key] = objective(replay(fam, ks, data, base, cfg, regime), rules, cfg)
        return memo[key]

    best = score(cur)
    for _ in range(ROUNDS):
        moved = False
        for name in spec["params"]:
            lo, hi = cfg["bounds"][name]
            for v in grid(lo, hi):
                trial = dict(cur, **{name: v})
                sc = score(trial)
                if sc[0] > best[0] + 1e-9:
                    best, cur, moved = sc, trial, True
        if not moved:
            break
    if log is not None:
        log.append(f"{fam}{'|' + regime if regime else ''}: 试了 {len(memo)} 组，最优 obj={best[0]:.1f} n={best[1]}")
    return cur, best


def clamp(prev, target, lo, hi, pct):
    a, b = prev * (1 - pct / 100), prev * (1 + pct / 100)
    v = min(max(target, min(a, b)), max(a, b))
    return round(min(max(v, lo), hi), 3)


# ----------------------------------------------------------------- 统计
def live_samples(regimes_prev):
    """logs/eval 里规则引擎信号（engine/catchup/backfill）的实盘结果。"""
    out = []
    if not os.path.isdir(EVAL_DIR):
        return out
    for fn in sorted(os.listdir(EVAL_DIR)):
        if not re.fullmatch(r"\d{8}\.json", fn):
            continue
        try:
            with open(os.path.join(EVAL_DIR, fn), encoding="utf-8") as f:
                j = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        day = f"{fn[:4]}-{fn[4:6]}-{fn[6:8]}"
        reg = regimes_prev.get(day)
        for s in j.get("signals") or []:
            if s.get("source") not in ("engine", "catchup", "backfill") or s.get("rule") not in ap.RULE_LEGS:
                continue
            if s.get("status") == "dup":
                continue
            e = s.get("eval") or {}
            if s.get("rule") in BUY_RULES:   # 交易仓买入类：按 +3% 止盈 / 持有 5 日的结果（evaluate.py tp_edge）
                g = e.get("tp_edge")
            else:
                g = e.get("t_pair_edge") if s.get("type", "").startswith("t_") and e.get("t_success") else e.get("edge_t1")
            if g is None:
                continue
            out.append((s["rule"], str(s.get("code")), day, s.get("time"), float(g), None, reg))
    return out


def build_stats(samples, cfg):
    k = cfg.get("shrink_k", 20)
    ref = cfg.get("ref_amount", 10000)
    groups = {}
    for rule, code, _d, _s, g, _n, reg in samples:
        kind = fees.kind_of(code)
        kind = "stock" if kind == "hk" else kind
        net = g - (fees.cost_pct(ref, code, ap.RULE_LEGS.get(rule, "round")) or 0)
        for key in ("all", kind, f"{kind}|{reg}" if reg else None):
            if key:
                groups.setdefault(rule, {}).setdefault(key, []).append((g, net))
    out = {}
    for rule, lv in groups.items():
        res = {}

        def node(key, parent):
            xs = lv.get(key)
            if not xs:
                return
            n = len(xs)
            gs = [x[0] for x in xs]
            raw = sum(gs) / n
            pm = parent["mean"] if parent else 0.0
            sd = statistics.pstdev(gs) if n >= 5 else (parent or {}).get("sd") or 2.0
            res[key] = {"n": n, "mean": round((n * raw + k * pm) / (n + k), 3), "mean_raw": round(raw, 3),
                        "sd": round(max(sd, 0.3), 3), "win": round(sum(1 for x in xs if x[1] > 0) / n, 3)}

        node("all", None)
        for kind in ("etf", "stock"):
            node(kind, res.get("all"))
            for reg in ap.REGIMES:
                node(f"{kind}|{reg}", res.get(kind) or res.get("all"))
        out[rule] = res
    return out


# ----------------------------------------------------------------- 主流程
def calibrate(offline=False, dry=False):
    base = st.load_params()
    cfg = ap.auto_cfg(base)
    pct = cfg.get("daily_change_pct", 30)
    codes = universe()
    m5, daily, hs, errs = load_all(codes, offline)
    regimes = regime_series(hs)
    data, atr_now = {}, {}
    for c in m5:
        a = atr_series(daily[c])
        if a:
            atr_now[c] = round(a[max(a)], 3)
        data[c] = build_states(c, m5[c], daily[c], a, regimes)
    data = {c: v for c, v in data.items() if v}
    all_days = sorted({d["date"] for v in data.values() for d in v})
    if not all_days:
        raise SystemExit("没有可回放的数据")
    reg_days = {r: len({d["date"] for v in data.values() for d in v if d["regime"] == r}) for r in ap.REGIMES}
    med_atr = statistics.median(atr_now.values())

    prev = None
    try:
        with open(ap.auto_path(base), encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, json.JSONDecodeError):
        prev = None
    baseline = ap.baseline_ks(base, med_atr)
    first = prev is None
    if prev and str((prev.get("data") or {}).get("to")) == all_days[-1].replace("-", "") and prev.get("prev"):
        # 同一数据日重复运行：以那次的前值为基准，避免一天内叠加两次 ±daily_change_pct
        first = bool(prev.get("first"))
        prev = {"ks": prev["prev"]}
    prev_pooled = dict(baseline, **((prev or {}).get("ks") or {}).get("pooled", {}))
    prev_reg = ((prev or {}).get("ks") or {}).get("by_regime") or {}

    log = []
    targets = {"pooled": {}, "by_regime": {}}
    final = {"pooled": {}, "by_regime": {}}
    fam_eval = {}
    for fam, spec in ap.FAMILIES.items():
        best, sc = search(fam, prev_pooled, data, base, cfg, None, log)
        for p in spec["params"]:
            targets["pooled"][p] = best[p]
            lo, hi = cfg["bounds"][p]
            final["pooled"][p] = clamp(prev_pooled[p], best[p], lo, hi, pct)
        for reg in ap.REGIMES:
            if reg_days[reg] < cfg.get("regime_min_days", 15):
                continue
            rb, _ = search(fam, best, data, base, cfg, reg, log)
            w = reg_days[reg] / (reg_days[reg] + cfg.get("shrink_k", 20))
            for p in spec["params"]:
                tgt = round(w * rb[p] + (1 - w) * best[p], 3)
                targets["by_regime"].setdefault(reg, {})[p] = tgt
                lo, hi = cfg["bounds"][p]
                pv = (prev_reg.get(reg) or {}).get(p, final["pooled"][p])
                final["by_regime"].setdefault(reg, {})[p] = clamp(pv, tgt, lo, hi, pct)
        rules = set(spec["rules"])
        fam_eval[fam] = {
            "prev": objective(replay(fam, prev_pooled, data, base, cfg), rules, cfg),
            "target": sc,
            "final": objective(replay(fam, final["pooled"], data, base, cfg), rules, cfg),
        }

    # 最终参数（按每天的市场状态取 k）下的全量样本 → 统计
    def ks_day(reg):
        return dict(final["pooled"], **final["by_regime"].get(reg, {}))

    samples = []
    for fam in list(ap.FAMILIES) + ["stop"]:
        samples.extend(replay(fam, ks_day, data, base, cfg))
    # 去掉不属于该组的止损重复（reduce 组也会带出止损）
    seen, uniq = set(), []
    for x in samples:
        key = x[:4]
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    prev_reg_map = {}
    hs_dates = [r["date"] for r in hs]
    for i, d in enumerate(hs_dates[1:], 1):
        r = regimes.get(hs_dates[i - 1])
        if r:
            prev_reg_map[d] = r["regime"]
    live = live_samples(prev_reg_map)
    stats = build_stats(uniq + live, cfg)

    last = max(regimes)
    auto = {
        "_说明": "calibrate.py 自动生成，勿手改；阈值 = k × 该标的 ATR20%（见 auto_params.py）",
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data": {"from": all_days[0].replace("-", ""), "to": all_days[-1].replace("-", ""),
                 "days": len(all_days), "codes": sorted(data), "regime_days": reg_days,
                 "samples_backtest": len(uniq), "samples_live": len(live), "errors": errs},
        "regime": {"asof": last, "next": regimes[last]["regime"], "detail": regimes[last]},
        "median_atr": round(med_atr, 3),
        "ks": final, "targets": targets, "prev": {"pooled": prev_pooled, "by_regime": prev_reg},
        "atr": atr_now, "stats": stats, "families": {k: {"prev": list(v["prev"]), "target": list(v["target"]),
                                                          "final": list(v["final"])} for k, v in fam_eval.items()},
        "sizing": cfg.get("sizing", {}), "search_log": log, "first": first,
    }
    report = render_report(auto, base, first=first)
    if not dry:
        with open(ap.auto_path(base), "w", encoding="utf-8") as f:
            json.dump(auto, f, ensure_ascii=False, indent=1)
        os.makedirs(REPORT_DIR, exist_ok=True)
        with open(os.path.join(REPORT_DIR, f"{datetime.date.today():%Y%m%d}.md"), "w", encoding="utf-8") as f:
            f.write(report)
    return auto, report


def _fmt_obj(o):
    obj, n, mean = o
    return f"n={n}，均值 {mean:+.2f}%" if mean is not None else f"n={n}（样本不足）"


def render_report(auto, base, first=False):
    reg = auto["regime"]
    med = auto["median_atr"]
    L = [f"# 阈值校准 {auto['generated_at']}", "",
         f"- 数据：{auto['data']['from']}~{auto['data']['to']}，{auto['data']['days']} 个交易日，"
         f"{len(auto['data']['codes'])} 只标的；回测样本 {auto['data']['samples_backtest']}，实盘样本 {auto['data']['samples_live']}",
         f"- 市场状态（下一交易日用）：**{ap.REGIME_NAME[reg['next']]}**（沪深300 {reg['detail']['close']}，"
         f"MA20 {reg['detail']['ma20']}，5 日斜率 {reg['detail']['ma20_slope5']:+}）；"
         f"回测期各状态天数 {auto['data']['regime_days']}",
         f"- 标的 ATR20% 中位数 {med}%（下表「约合」按此换算）" + ("；**首次校准**，前值 = 固定阈值折算" if first else ""),
         ""]
    if auto["data"]["errors"]:
        L += ["- 数据问题：" + "；".join(auto["data"]["errors"]), ""]
    L += ["## 参数（前值 → 回测最优 → 生效，单日限幅 ±%s%%）" % ap.auto_cfg(base).get("daily_change_pct", 30), "",
          "| 参数 | 含义 | 前值 | 目标 | 生效 | 约合 |", "|---|---|---|---|---|---|"]
    changes = []
    for name in ap.TUNABLES:
        unit = ap.TUNABLES[name][2]
        pv, tg, fv = auto["prev"]["pooled"].get(name), auto["targets"]["pooled"].get(name), auto["ks"]["pooled"].get(name)
        approx = f"{fv * med:.2f}%" if unit == "atr" and fv is not None else ""
        L.append(f"| {name} | {DESC[name]} | {pv} | {tg} | {fv} | {approx} |")
        if pv and fv is not None and abs(fv / pv - 1) >= 0.05:
            changes.append(f"{DESC[name]} {pv}→{fv}")
    for r, ks in auto["ks"]["by_regime"].items():
        L += ["", f"{ap.REGIME_NAME[r]}市（覆盖上表）：" + "，".join(f"{k}={v}" for k, v in ks.items())]
    L += ["", "## 各规则组回测（每笔按 1 万元扣手续费）", "", "| 组 | 前值 | 生效值 |", "|---|---|---|"]
    for fam, v in auto["families"].items():
        L.append(f"| {fam} | {_fmt_obj(v['prev'])} | {_fmt_obj(v['final'])} |")
    L += ["", "## EV 统计（毛收益，已分层收缩；EV = 均值 − 实际手续费）", "",
          "| 规则 | 层 | 样本 | 毛均值 | 原始均值 | 标准差 | 扣费胜率 |", "|---|---|---|---|---|---|---|"]
    for rule, lv in auto["stats"].items():
        for key in ["all", "etf", "stock"] + [f"{k}|{r}" for k in ("etf", "stock") for r in ap.REGIMES]:
            s = lv.get(key)
            if s:
                L.append(f"| {RULE_NAME.get(rule, rule)} | {key} | {s['n']} | {s['mean']:+.2f}% | "
                         f"{s['mean_raw']:+.2f}% | {s['sd']:.2f} | {s['win']:.0%} |")
    L += ["", "## 摘要（复盘消息用）", "", summary_line(auto, changes)]
    return "\n".join(L) + "\n"


def summary_line(auto, changes=None):
    reg = ap.REGIME_NAME[auto["regime"]["next"]]
    if changes is None:
        changes = []
        for name in ap.TUNABLES:
            pv, fv = auto["prev"]["pooled"].get(name), auto["ks"]["pooled"].get(name)
            if pv and fv is not None and abs(fv / pv - 1) >= 0.05:
                changes.append(f"{DESC[name]} {pv}→{fv}")
    neg = []
    for rule, lv in auto["stats"].items():
        s = lv.get("all")
        if s and s["mean"] <= 0.1:
            neg.append(RULE_NAME.get(rule, rule))
    return (f"明日市场状态：{reg}；参数变化：{'；'.join(changes) if changes else '无明显变化（<5%）'}；"
            f"回测毛收益偏弱（≤0.1%）的规则：{'、'.join(neg) if neg else '无'}")


# ----------------------------------------------------------------- 子命令
def _ctx_from_snapshot(base, auto):
    snap = {}
    for p in (se.SNAP_LIVE, se.SNAP_PRE):
        try:
            with open(p, encoding="utf-8") as f:
                snap = json.load(f)
            break
        except (OSError, json.JSONDecodeError):
            continue
    return se.engine_ctx(snap, base, auto)


def cmd_ev(args):
    def opt(n, d=None):
        return args[args.index(n) + 1] if n in args and args.index(n) + 1 < len(args) else d
    base = st.load_params()
    auto = ap.load_auto(base)
    if not auto:
        print(json.dumps({"error": "rules_auto.json 缺失/过期/未启用，无法算 EV"}, ensure_ascii=False))
        return 1
    code, t = opt("--code"), st.norm_type(opt("--type", ""))
    price = _f(opt("--price"))
    rule = opt("--rule") or TYPE_RULE.get(t)
    if not code or not price or not rule:
        print("用法：calibrate.py ev --code 159813 --type add --price 0.53 [--qty 10000] [--available 5000]")
        return 2
    ctx = _ctx_from_snapshot(base, auto)
    qty = _f(opt("--qty"))
    avail = _f(opt("--available"))
    if avail is None:
        avail = qty if t in ap.SELL_TYPES or t == "t_low" else None
    c = {"type": t, "rule": rule, "code": code, "price": price, "qty": int(qty) if qty else None,
         "reason": "大模型补充", "metrics": {"available": avail}, "flags": []}
    ap.size_and_ev(c, ctx)
    out = {k: c.get(k) for k in ("code", "type", "rule", "price", "qty", "status", "ev", "flags", "reason")}
    out["regime"] = ctx["regime"]
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_show():
    base = st.load_params()
    auto = ap.load_auto(base)
    if not auto:
        print("rules_auto.json 缺失/过期/未启用：引擎使用 rules_params.json 固定阈值")
        return 0
    reg = ap.regime_of(auto)
    ks = ap.ks_for(auto, reg)
    ctx = _ctx_from_snapshot(base, auto)
    print(f"校准于 {auto['generated_at']}（数据 {auto['data']['from']}~{auto['data']['to']}），"
          f"市场状态 {ap.REGIME_NAME[reg]}")
    print("k：" + "，".join(f"{k}={v}" for k, v in ks.items()))
    print(summary_line(auto))
    sz = auto.get("sizing") or {}
    TA, cash = ctx["total_assets"] or 0, ctx["cash"] or 0
    if TA:
        print(f"资金：总资产 {TA:.0f}，可用 {cash:.0f}，现金底线 {sz.get('cash_floor', 0):.0%} → 可买空间 "
              f"{max(0, cash - sz.get('cash_floor', 0) * TA):.0f} 元；单只上限 {sz.get('position_cap', 0.2) * TA:.0f} 元")
    bsz = (base.get("auto") or {}).get("sizing") or {}
    if bsz.get("max_positions"):
        dust = "、".join(f"{d['code']}({d['value']:.0f})" for d in ctx.get("dust") or [])
        print(f"长线仓只数：持有 {ctx['n_positions']} 只 / 上限 {bsz['max_positions']} 只；"
              f"碎仓（<{bsz.get('dust_yuan', 10000):.0f} 元）：{dust or '无'}")
    if ctx.get("trade_budget"):
        tv, tb = ctx.get("trade_value") or 0, ctx["trade_budget"]
        tc = [c for c, v in (ctx.get("sleeves") or {}).items() if v == "trade"]
        print(f"交易仓（不限只数）：市值 {tv:.0f} / 预算 {tb:.0f} 元"
              + (f"，超出 {tv - tb:.0f} 元 → 只卖不买" if tv > tb else f"，还可买 {tb - tv:.0f} 元")
              + (f"；标的 {'、'.join(tc)}" if tc else ""))
    for c, pv in ctx["pos_value"].items():
        a = ap.atr_of(auto, c)
        p = ap.effective(base, ks, a)
        room = sz.get("position_cap", 0.2) * TA - pv if TA else None
        print(f"  {c} ATR {a}%：做T振幅≥{p['t']['min_amplitude_pct']}% 加仓回升≥{p['add']['rebound_pct']}% "
              f"减仓回升≥{p['reduce']['rebound_pct']}%；市值 {pv:.0f}，距单只上限 {room:.0f}")
    return 0


def main(argv):
    args = list(argv)
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0
    if args and args[0] == "ev":
        return cmd_ev(args[1:])
    if args and args[0] == "show":
        return cmd_show()
    t0 = datetime.datetime.now()
    auto, report = calibrate(offline="--offline" in args, dry="--dry-run" in args)
    print(report)
    print(f"耗时 {(datetime.datetime.now() - t0).seconds} 秒" + ("（dry-run，未写文件）" if "--dry-run" in args else
                                                              f"；已写 {ap.auto_cfg(st.load_params()).get('file')} 与 logs/calib/"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
