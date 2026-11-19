#!/usr/bin/env python3
"""收盘后评估：给当天（及近几个交易日）的每条建议打分，生成评估报告。

用法：
  python3 main.py evaluate                  # 评估今天，并刷新近 8 个交易日信号的 T+1/T+3/T+5 结果
  python3 main.py evaluate --date 20261030  # 评估指定日期
  python3 main.py evaluate --quiet          # 只写文件，标准输出只打印简报

数据：
  信号   data/report/signals/yyyyMMdd.jsonl（规则候选 + 大模型判断 + 发送状态）
  成交   operations.csv（张小凡的实际操作，用来算执行偏差、识别自主操作）
  行情   当天分钟价（腾讯 minute/query）；历史日 A 股用 5 分钟 K；港股历史日只有日 K（标注「近似」）
         T+N 用日 K 收盘价；基准：A股/ETF → 沪深300，港股 → 恒生指数

输出：
  data/report/eval/yyyyMMdd.json   每条信号的评估明细（每次运行会刷新）
  data/report/eval/yyyyMMdd.md     当日评估报告
  data/report/eval/summary.md      近 20 个交易日按规则/类型/来源/判断的累计统计

评分口径（方向 side：买入类 +1，卖出/回避买入类 -1，异动 0 不评方向）：
  edge_close   = side × (当日收盘 / 信号价 − 1)，即按建议方向操作到收盘的收益
  edge_tN      = side × (T+N 收盘 / 信号价 − 1)
  excess_*     = 扣掉同期基准涨跌后的超额
  zone_hit     信号之后价格是否进入建议区间（当日 / 次日）
  做T          价格先进入建议区间（高抛挂区间下沿、低吸挂区间上沿）才算触发；当天没到价 ⚪（挂单当日有效，不计胜率）。
               有成对区间（pair_zone，2026-11-02 起做T必须成对）：触发后价格到达回补区上沿/卖回区下沿
               （当日或次日）为 ✅「回补成交」，edge = 两腿价差；没到 ❌「回补未成交」（即单边成了减仓/加仓）。
               旧信号没有成对区间：触发后从成交价回落/反弹 ≥1% 为 ✅，否则 ❌。
  尾盘信号     14:50 之后的信号不按收盘评，等 T+1
  verdict      ✅ 对 / ❌ 错 / ➖ 持平（|edge| < 0.3%）/ ⏳ 待定；主口径：T+1 有了用 T+1，否则用收盘（暂评）
"""
import csv
import datetime
import glob
import json
import os
import sys

import signal_store as st
from datasources import quotes
from datasources._http import http_json, DataSourceError, normalize

from paths import ROOT
from datamul import data_path
EVAL_DIR = os.path.join(ROOT, "data", "report", "eval")
OPS_CSV = data_path("operations.csv")
HOSTS = ("https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn", "https://proxy.finance.qq.com/ifzqgtimg")
BENCH = {"A": "sh000300", "HK": "hkHSI"}
BENCH_NAME = {"sh000300": "沪深300", "hkHSI": "恒生指数"}


# ----------------------------------------------------------------- 行情
def _get(path):
    last = None
    for h in HOSTS:
        try:
            return http_json(h + path, retries=0)
        except DataSourceError as e:
            last = e
    raise last


_cache = {}


def tsym(code):
    if code in BENCH.values():
        return code
    return normalize(code)["tencent"]


def is_hk(code):
    return tsym(code).startswith("hk")


def daily(code, count=30):
    """[(yyyymmdd, open, close, high, low)]"""
    key = ("d", code)
    if key not in _cache:
        adj = "" if code in BENCH.values() else "qfq"
        rows = quotes.kline(code, "day", count, adj)   # 腾讯 newfqkline，失败回退东财
        _cache[key] = [(r["date"].replace("-", ""), r["open"], r["close"], r["high"], r["low"]) for r in rows]
    return _cache[key]


def minute_today(code):
    """当天分钟价 [(HHMM, high, low, close)]，返回 (date, bars)。"""
    key = ("m", code)
    if key not in _cache:
        s = tsym(code)
        j = _get(f"/appstock/app/minute/query?code={s}")
        d = ((j.get("data") or {}).get(s) or {}).get("data") or {}
        bars = []
        for row in d.get("data") or []:
            parts = row.split()
            if len(parts) >= 2:
                p = float(parts[1])
                bars.append((parts[0], p, p, p))
        _cache[key] = (str(d.get("date", "")), bars)
    return _cache[key]


def m5(code, count=400):
    """A 股 5 分钟 K：{yyyymmdd: [(HHMM, high, low, close)]}"""
    key = ("m5", code)
    if key not in _cache:
        s = tsym(code)
        j = _get(f"/appstock/app/kline/mkline?param={s},m5,,{count}")
        rows = ((j.get("data") or {}).get(s) or {}).get("m5") or []
        out = {}
        for r in rows:
            out.setdefault(r[0][:8], []).append((r[0][8:12], float(r[3]), float(r[4]), float(r[2])))
        _cache[key] = out
    return _cache[key]


def intraday_bars(code, day):
    """返回 (bars, approx)。bars 为当天按时间排序的 (HHMM, high, low, close)。"""
    try:
        d, bars = minute_today(code)
        if d == day and bars:
            return bars, False
    except (DataSourceError, ValueError, KeyError):
        pass
    if not is_hk(code):
        try:
            bars = m5(code).get(day)
            if bars:
                return bars, False
        except (DataSourceError, ValueError, KeyError):
            pass
    for r in daily(code):
        if r[0] == day:  # 只有日 K：用开盘作 09:30，高低收作全天
            return [("0930", r[1], r[1], r[1]), ("1500", r[3], r[4], r[2])], True
    return [], True


def price_at(bars, hhmm):
    """不晚于 hhmm 的最后一根价格；没有则取第一根。"""
    best = None
    for b in bars:
        if b[0] <= hhmm:
            best = b[3]
    return best if best is not None else (bars[0][3] if bars else None)


# ----------------------------------------------------------------- 评估
def _r(x, n=2):
    return None if x is None else round(x, n)


def evaluate_signal(sig, day, tdays, params):
    E = params["eval"]
    code, side = sig["code"], st.side_of(sig["type"])
    t0 = (sig.get("time") or "09:30").replace(":", "")
    res = {"evaluated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    try:
        bars, approx = intraday_bars(code, day)
    except Exception as e:  # noqa: BLE001
        return {**res, "error": f"取行情失败: {e}"}
    if not bars:
        return {**res, "error": "无当日行情"}
    res["approx"] = approx
    p0 = sig.get("price")
    if not p0:
        nxt = [b for b in bars if b[0] >= t0]
        p0 = (nxt[0] if nxt else bars[0])[3]
        res["price_filled"] = True
    res["p0"] = p0
    after = [b for b in bars if b[0] > t0]
    close_d = bars[-1][3]
    res["close"] = close_d
    if after:
        hi, lo = max(b[1] for b in after), min(b[2] for b in after)
        res["after_high"], res["after_low"] = hi, lo
        up, dn = (hi / p0 - 1) * 100, (lo / p0 - 1) * 100
        if side >= 0:
            res["mfe"], res["mae"] = _r(up), _r(dn)
        else:
            res["mfe"], res["mae"] = _r(-dn), _r(-up)

    # 基准
    bsym = BENCH["HK" if is_hk(code) else "A"]
    res["bench"] = BENCH_NAME[bsym]
    try:
        bbars, _ = intraday_bars(bsym, day)
        b0, bclose = price_at(bbars, t0), bbars[-1][3]
    except Exception:  # noqa: BLE001
        b0 = bclose = None

    raw_close = (close_d / p0 - 1) * 100
    res["move_close"] = _r(raw_close)
    if side:
        res["edge_close"] = _r(side * raw_close)
        if b0 and bclose:
            res["excess_close"] = _r(side * (raw_close - (bclose / b0 - 1) * 100))

    # 后续交易日
    try:
        dk = {r[0]: r for r in daily(code)}
        bk = {r[0]: r for r in daily(bsym)}
    except Exception:  # noqa: BLE001
        dk, bk = {}, {}
    later = [d for d in tdays if d > day]
    for n in E["horizons"]:
        if len(later) >= n and later[n - 1] in dk:
            dn_ = later[n - 1]
            mv = (dk[dn_][2] / p0 - 1) * 100
            res[f"move_t{n}"] = _r(mv)
            if side:
                res[f"edge_t{n}"] = _r(side * mv)
                if b0 and dn_ in bk:
                    res[f"excess_t{n}"] = _r(side * (mv - (bk[dn_][2] / b0 - 1) * 100))
    d1 = dk.get(later[0]) if later else None

    # 交易仓买入类带止盈价 tp_price：次日起 hold_days 个交易日内最高价到价记止盈收益，否则按第 hold_days 日收盘
    if sig.get("tp_price") and side > 0:
        tp, hold = sig["tp_price"], int(sig.get("hold_days") or 5)
        win = [d for d in later[:hold] if d in dk]
        hit = next((d for d in win if dk[d][3] >= tp), None)
        if hit:
            res["tp_edge"], res["tp_hit"] = _r((tp / p0 - 1) * 100), hit
        elif len(win) >= hold:
            res["tp_edge"], res["tp_hit"] = _r((dk[win[-1]][2] / p0 - 1) * 100), None

    # 区间命中
    z = sig.get("zone")
    if z and side:
        lo_z, hi_z = z
        if side > 0:
            hit = p0 <= hi_z or (after and min(b[2] for b in after) <= hi_z)
            hit1 = bool(d1 and d1[4] <= hi_z)
        else:
            hit = p0 >= lo_z or (after and max(b[1] for b in after) >= lo_z)
            hit1 = bool(d1 and d1[3] >= lo_z)
        res["zone_hit"] = bool(hit)
        if d1:
            res["zone_hit_t1"] = hit1

    # 做T：先要价格进入建议区间（挂单成交），再从成交价回转 ≥ t_target 才算成功
    if sig["type"] in ("t_high", "t_low"):
        tgt = E["t_target_pct"] / 100
        hi_side = sig["type"] == "t_high"
        lvl = (z[0] if hi_side else z[1]) if z else p0   # 高抛挂区间下沿、低吸挂区间上沿
        entry, ok, when = None, False, None
        if (hi_side and p0 >= lvl) or (not hi_side and p0 <= lvl):
            entry, idx = p0, 0
        else:
            idx = None
            for i, b in enumerate(after):
                if (hi_side and b[1] >= lvl) or (not hi_side and b[2] <= lvl):
                    entry, idx = lvl, i + 1
                    break
        pair = sig.get("pair_zone")
        if pair:   # 成对：第二腿挂在回补区上沿 / 卖回区下沿
            plvl = max(pair) if hi_side else min(pair)
            res["t_pair_level"] = plvl
        if entry is not None:
            rest = after[idx:] if idx else after
            if pair:
                ok = any((b[2] <= plvl) if hi_side else (b[1] >= plvl) for b in rest)
            elif hi_side:
                ok = any(b[2] <= entry * (1 - tgt) for b in rest)
            else:
                ok = any(b[1] >= entry * (1 + tgt) for b in rest)
            when = "当日" if ok else None
            if not ok and d1:
                if pair:
                    ok = (d1[4] <= plvl) if hi_side else (d1[3] >= plvl)
                else:
                    ok = (d1[4] <= entry * (1 - tgt)) if hi_side else (d1[3] >= entry * (1 + tgt))
                when = "次日" if ok else None
        res["t_entry"] = entry
        res["t_success"] = bool(ok)
        res["t_success_when"] = when
        if entry is not None:
            mv = (close_d / entry - 1) * 100
            res["edge_close"] = _r(side * mv)
            if b0 and bclose:
                res["excess_close"] = _r(side * (mv - (bclose / b0 - 1) * 100))
        if entry is None:   # 做T挂单当日有效，当天没到价就是没成交；没成交就没有收益可算
            for k in ("edge_close", "excess_close", "edge_t1", "excess_t1", "edge_t3", "excess_t3", "edge_t5", "excess_t5"):
                res.pop(k, None)
            res["verdict"], res["verdict_basis"] = "⚪", "未触发"
        elif ok:
            res["verdict"], res["verdict_basis"] = "✅", f"{when}{'回补成交' if pair else '回转'}"
            if pair:   # 两腿都成交：收益 = 两腿价差
                res["t_pair_edge"] = _r(side * (plvl / entry - 1) * 100)
        else:
            res["verdict"] = "❌" if d1 else "⏳"
            res["verdict_basis"] = (("回补未成交" if hi_side else "卖回未成交") if pair else "触发未回转") \
                if d1 else "触发待次日"
        return res

    if not side:
        res["verdict"], res["verdict_basis"] = "—", "异动不评方向"
        return res
    band = E["flat_band_pct"]
    if res.get("edge_t1") is not None:
        e, basis = res["edge_t1"], "T+1"
    elif t0 >= "1450":   # 尾盘信号到收盘没几分钟，必须等 T+1
        res["verdict"], res["verdict_basis"] = "⏳", "尾盘待T+1"
        return res
    else:
        e, basis = res.get("edge_close"), "收盘(暂评)"
    if e is None:
        res["verdict"], res["verdict_basis"] = "⏳", basis
    else:
        res["verdict"] = "✅" if e > band else ("❌" if e < -band else "➖")
        res["verdict_basis"] = basis
    return res


def load_ops(day):
    if not os.path.exists(OPS_CSV):
        return []
    d = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    out = []
    with open(OPS_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("日期") == d:
                code = (r.get("代码") or "").upper().replace(".HK", "")
                if code.isdigit() and len(code) <= 5:
                    code = code.zfill(5)
                out.append({"time": r.get("时间", ""), "op": r.get("操作", ""), "name": r.get("名称", ""),
                            "code": code, "qty": float(r.get("数量") or 0), "price": float(r.get("价格") or 0),
                            "side": 1 if "买" in r.get("操作", "") else -1, "note": r.get("备注", "")})
    return out


def _zone_pos(op, lo, hi):
    """成交价相对建议区间：True 在区间内；"better" 在区间外但更有利（买得更低/卖得更高）；False 区间外不利。"""
    if lo <= op["price"] <= hi:
        return True
    if (op["side"] > 0 and op["price"] < lo) or (op["side"] < 0 and op["price"] > hi):
        return "better"
    return False


def match_ops(ops, sigs, day, tdays):
    """把每笔成交匹配到它之前最近的同标的同方向信号。

    做T信号的反向腿（高抛的回补、低吸的卖回）也算匹配，标「回补腿/卖回腿」，在区间内按 pair_zone 判；
    倒序做T（先卖回再低吸）同样匹配到卖回腿。匹配之后、成交之前若出现同标的反方向的非做T信号
    （如加仓后又发了暂不加仓/止损），标「违背 HH:MM 信号」。
    """
    out = []
    for op in ops:
        live = [s for s in sigs if s["code"] == op["code"] and (s.get("time") or "") <= op["time"]
                and s.get("status") == "new" and s.get("decision") != "reject"]
        cands = []
        for s in live:
            sd = st.side_of(s["type"])
            if sd == op["side"]:
                cands.append((s, False))
            elif s["type"] in st.T_TYPES and s.get("pair_zone") and sd == -op["side"]:
                cands.append((s, True))
        cands.sort(key=lambda x: (bool(x[0].get("sent")), x[0].get("time", "")))
        m, leg = cands[-1] if cands else (None, False)
        r = dict(op)
        if m:
            r["matched"] = m["id"]
            if leg:
                r["matched_label"] = f"{m.get('time')} {m.get('label')}·{'回补腿' if m['type'] == 't_high' else '卖回腿'}"
                z = m.get("pair_zone")
                lo, hi = min(z), max(z)
                ref = hi if op["side"] > 0 else lo
                r["exec_vs_signal"] = _r(-op["side"] * (op["price"] / ref - 1) * 100)
                r["in_zone"] = _zone_pos(op, lo, hi)
            else:
                p0 = m.get("eval", {}).get("p0") or m.get("price")
                r["matched_label"] = f"{m.get('time')} {m.get('label')}"
                if p0:
                    r["exec_vs_signal"] = _r(-op["side"] * (op["price"] / p0 - 1) * 100)
                z = m.get("zone")
                if z:
                    r["in_zone"] = _zone_pos(op, min(z), max(z))
        after = (m.get("time") or "") if m else ""
        against = [s for s in live if st.side_of(s["type"]) == -op["side"] and s["type"] not in st.T_TYPES
                   and (s.get("time") or "") >= after and (not m or s["id"] != m["id"])]
        if against:
            a = max(against, key=lambda s: s.get("time", ""))
            r["against"] = f"{a.get('time')} {a.get('label')}"
            r["matched_label"] = (r.get("matched_label", "自主操作") + f"（违背 {r['against']}）")
        try:
            bars, _ = intraday_bars(op["code"], day)
            close_d = bars[-1][3] if bars else None
            dk = {x[0]: x for x in daily(op["code"])}
            later = [d for d in tdays if d > day]
            if close_d:
                r["edge_close"] = _r(op["side"] * (close_d / op["price"] - 1) * 100)
            if later and later[0] in dk:
                r["edge_t1"] = _r(op["side"] * (dk[later[0]][2] / op["price"] - 1) * 100)
        except Exception:  # noqa: BLE001
            pass
        out.append(r)
    return out


# ----------------------------------------------------------------- 报告
def _fmt(x, suffix="%"):
    return "—" if x is None else f"{x:+.2f}{suffix}"


def _src_name(s):
    return {"engine": "规则", "catchup": "规则(补跑)", "backfill": "规则(回放)",
            "llm": "大模型", "log": "日志导入"}.get(s.get("source"), s.get("source"))


def _dec_name(s):
    if s.get("status") in ("blocked", "group_suppressed", "ev_negative", "slots_full"):
        return {"blocked": "屏蔽", "group_suppressed": "二选一落选", "ev_negative": "EV≤0屏蔽",
                "slots_full": "只数已满"}[s["status"]]
    if s.get("source") in ("engine", "catchup", "backfill"):
        return {"accept": "采纳", "reject": "否决"}.get(s.get("decision"), "未判断")
    return "大模型提出"


def write_day_report(day, sigs, ops):
    lines = [f"# {day[:4]}-{day[4:6]}-{day[6:]} 建议评估", ""]
    lines.append(f"> 生成于 {datetime.datetime.now():%Y-%m-%d %H:%M}。口径见 evaluate.py 文件头；"
                 "✅对 ❌错 ➖持平 ⏳待定 ⚪未触发(不计)；edge 为按建议方向操作的收益，超额已扣基准。")
    lines.append("")
    effective = [s for s in sigs if s.get("status") == "new"]
    scored = [s for s in effective if s.get("eval", {}).get("verdict") in ("✅", "❌", "➖")]
    win = sum(1 for s in scored if s["eval"]["verdict"] == "✅")
    lose = sum(1 for s in scored if s["eval"]["verdict"] == "❌")
    sent = [s for s in effective if s.get("sent")]
    lines.append("## 概览")
    lines.append(f"- 信号 {len(effective)} 条（已发通知 {len(sent)} 条），可评方向 {len(scored)} 条："
                 f"✅ {win} / ❌ {lose} / ➖ {len(scored) - win - lose}")
    zs = [s for s in effective if s.get("eval", {}).get("zone_hit") is not None]
    if zs:
        lines.append(f"- 建议价位当日触达率 {sum(1 for s in zs if s['eval']['zone_hit'])}/{len(zs)}")
    eds = [s["eval"]["edge_close"] for s in effective if s.get("eval", {}).get("edge_close") is not None]
    if eds:
        lines.append(f"- 按建议方向操作到收盘的平均收益 {sum(eds) / len(eds):+.2f}%")
    lines.append("")
    lines.append("## 逐条")
    lines.append("| 时间 | 信号 | 标的 | 信号价 | 建议区间 | 来源/判断 | 发送 | 触达 | 收盘 | 到收盘 | 超额 | T+1 | 结论 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for s in sigs:
        e = s.get("eval", {})
        z = s.get("zone")
        zt = f"{z[0]}~{z[1]}" if z else "—"
        if st.pair_text(s):
            zt += f"（{st.pair_text(s)}）"
        hit = e.get("zone_hit")
        hit_t = "—" if hit is None else ("是" if hit else ("次日" if e.get("zone_hit_t1") else "否"))
        tag = "" if s.get("status") == "new" else "（未计入）"
        if e.get("error"):
            lines.append(f"| {s.get('time')} | {s.get('label')} | {s.get('name')} | {s.get('price')} | {zt} | "
                         f"{_src_name(s)}/{_dec_name(s)} | | | | | | | 行情缺失{tag} |")
            continue
        extra = ""
        if s["type"] in ("t_high", "t_low"):
            extra = f" {e.get('verdict_basis', '')}"
        lines.append(
            f"| {s.get('time')} | {s.get('label')} | {s.get('name')} | {e.get('p0')}{'*' if e.get('price_filled') else ''} | {zt} | "
            f"{_src_name(s)}/{_dec_name(s)} | {'是' if s.get('sent') else ''} | {hit_t} | {e.get('close')} | "
            f"{_fmt(e.get('edge_close') if s.get('side') else e.get('move_close'))} | {_fmt(e.get('excess_close'))} | "
            f"{_fmt(e.get('edge_t1'))} | {e.get('verdict', '')}{extra}{'≈' if e.get('approx') else ''}{tag} |")
    lines.append("")
    lines.append("注：信号价带 * 表示原建议没给现价（如盘前建议），按该时点之后第一笔价格计；≈ 表示只有日 K、按全天高低近似；"
                 "异动信号只记录涨跌（到收盘列为原始涨跌），不评对错。")
    lines.append("")
    if ops:
        lines.append("## 实际操作对照（operations.csv）")
        lines.append("| 时间 | 操作 | 标的 | 成交价 | 对应信号 | 相对信号价 | 在区间内 | 到收盘 | T+1 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for o in ops:
            lines.append(f"| {o['time']} | {o['op']} | {o['name']} | {o['price']} | {o.get('matched_label', '自主操作')} | "
                         f"{_fmt(o.get('exec_vs_signal'))} | {'' if 'in_zone' not in o else ('更优' if o['in_zone'] == 'better' else ('是' if o['in_zone'] else '否'))} | "
                         f"{_fmt(o.get('edge_close'))} | {_fmt(o.get('edge_t1'))} |")
        lines.append("")
        lines.append("注：「相对信号价」为正表示成交价比信号价更有利（卖得更高/买得更低）；「到收盘」为这笔操作持有到收盘的收益（卖出则为少亏/多赚的部分）。")
        lines.append("")
    # 大模型过滤效果
    eng = [s for s in sigs if s.get("source") in ("engine", "catchup", "backfill") and s.get("side")
           and s.get("eval", {}).get("edge_close") is not None]
    if eng:
        lines.append("## 规则候选 × 大模型判断")
        for name, flt in (("采纳", lambda s: s.get("decision") == "accept"),
                          ("否决", lambda s: s.get("decision") == "reject"),
                          ("未判断", lambda s: s.get("decision") is None and s.get("status") == "new"),
                          ("屏蔽/落选", lambda s: s.get("status") != "new")):
            g = [s["eval"]["edge_close"] for s in eng if flt(s)]
            if g:
                lines.append(f"- {name}：{len(g)} 条，到收盘平均 {sum(g) / len(g):+.2f}%")
        lines.append("")
    os.makedirs(EVAL_DIR, exist_ok=True)
    path = os.path.join(EVAL_DIR, f"{day}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _group_stats(items, key):
    g = {}
    for s in items:
        g.setdefault(key(s), []).append(s)
    rows = []
    for k, lst in sorted(g.items(), key=lambda kv: -len(kv[1])):
        e = lst[0]["eval"]
        sc = [s for s in lst if s["eval"].get("verdict") in ("✅", "❌", "➖")]
        win = sum(1 for s in sc if s["eval"]["verdict"] == "✅")

        def avg(f):
            v = [s["eval"].get(f) for s in lst if s["eval"].get(f) is not None]
            return (sum(v) / len(v)) if v else None
        zs = [s for s in lst if s["eval"].get("zone_hit") is not None]
        rows.append((k, len(lst), f"{win}/{len(sc)}" if sc else "—",
                     f"{sum(1 for s in zs if s['eval']['zone_hit'])}/{len(zs)}" if zs else "—",
                     avg("edge_close"), avg("excess_close"), avg("edge_t1"), avg("edge_t5")))
    return rows


def write_summary(params):
    n = params["eval"]["summary_days"]
    files = sorted(glob.glob(os.path.join(EVAL_DIR, "2*.json")))[-n:]
    items = []
    for p in files:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        for s in d.get("signals", []):
            if s.get("eval") and not s["eval"].get("error") and s.get("side"):
                items.append(s)
    live = [s for s in items if s.get("status") == "new"]   # 被屏蔽/落选的只在「来源与判断」里看
    lines = [f"# 建议评估累计统计（近 {len(files)} 个交易日，{len(live)} 条有方向的有效信号，另有屏蔽/落选 {len(items) - len(live)} 条）", "",
             f"> 生成于 {datetime.datetime.now():%Y-%m-%d %H:%M}。胜率=✅/(✅+❌+➖)；触达=信号后价格进入建议区间；"
             "收益均为按建议方向操作的平均值，超额已扣基准。样本少于 20 条的分组结论不稳定。", ""]
    hdr = "| {} | 条数 | 胜率 | 区间触达 | 到收盘 | 超额 | T+1 | T+5 |"
    for title, key, pool in (("按信号类型", lambda s: s.get("label"), live),
                             ("按规则", lambda s: s.get("rule") or "—", live),
                             ("按来源与判断", lambda s: f"{_src_name(s)}/{_dec_name(s)}", items),
                             ("按是否发送", lambda s: "已发通知" if s.get("sent") else "未发", live)):
        lines += [f"## {title}", hdr.format(title[1:]), "|---|---|---|---|---|---|---|---|"]
        for k, c, w, z, a, ex, t1, t5 in _group_stats(pool, key):
            lines.append(f"| {k} | {c} | {w} | {z} | {_fmt(a)} | {_fmt(ex)} | {_fmt(t1)} | {_fmt(t5)} |")
        lines.append("")
    os.makedirs(EVAL_DIR, exist_ok=True)
    path = os.path.join(EVAL_DIR, "summary.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path, items


def trading_days_str():
    try:
        return [r[0] for r in daily("sh000300", 60)]
    except Exception:  # noqa: BLE001
        return []


def evaluate_day(day, tdays, params):
    sigs = list(st.load_signals(day).values())
    if not sigs:
        return None
    for s in sigs:
        s["side"] = st.side_of(s["type"])
        s["eval"] = evaluate_signal(s, day, tdays, params)
    ops = match_ops(load_ops(day), sigs, day, tdays)
    os.makedirs(EVAL_DIR, exist_ok=True)
    with open(os.path.join(EVAL_DIR, f"{day}.json"), "w", encoding="utf-8") as f:
        json.dump({"date": day, "evaluated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                   "signals": sigs, "ops": ops}, f, ensure_ascii=False, indent=1, default=str)
    write_day_report(day, sigs, ops)
    return sigs, ops


def brief(day, sigs, ops):
    eff = [s for s in sigs if s.get("status") == "new"]
    sc = [s for s in eff if s["eval"].get("verdict") in ("✅", "❌", "➖")]
    win = sum(1 for s in sc if s["eval"]["verdict"] == "✅")
    lose = sum(1 for s in sc if s["eval"]["verdict"] == "❌")
    out = [f"【建议评估 {day[4:6]}-{day[6:]}】信号 {len(eff)} 条，可评 {len(sc)} 条：✅{win} ❌{lose} ➖{len(sc) - win - lose}"]
    good = sorted([s for s in sc if s.get("sent")], key=lambda s: s["eval"].get("edge_close") or 0)
    if good:
        b, w = good[-1], good[0]
        out.append(f"最好：{b['time']} {b['label']} {b['name']} {_fmt(b['eval'].get('edge_close'))}")
        out.append(f"最差：{w['time']} {w['label']} {w['name']} {_fmt(w['eval'].get('edge_close'))}")
    for o in ops:
        out.append(f"操作 {o['time']} {o['op']}{o['name']}@{o['price']}：{o.get('matched_label', '自主操作')}，"
                   f"相对信号价 {_fmt(o.get('exec_vs_signal'))}，到收盘 {_fmt(o.get('edge_close'))}")
    return "\n".join(out)


def main(argv):
    args = list(argv)
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0
    day = None
    if "--date" in args:
        day = args[args.index("--date") + 1]
    params = st.load_params()
    tdays = trading_days_str()
    today = datetime.date.today().strftime("%Y%m%d")
    day = day or today
    if tdays and day == today and day not in tdays:
        print(f"{day} 非交易日，跳过评估")
        return 0
    # 刷新近 N 个交易日（补 T+1/T+3/T+5）
    lb = params["eval"]["lookback_days"]
    prev = [d for d in tdays if d < day][-lb:]
    for d in prev:
        if os.path.exists(st.day_path(d)):
            evaluate_day(d, tdays, params)
    r = evaluate_day(day, tdays, params)
    path_sum, _ = write_summary(params)
    if not r:
        print(f"{day} 没有信号记录（data/report/signals/{day}.jsonl 不存在），只刷新了历史评估与 {path_sum}")
        return 0
    sigs, ops = r
    print(brief(day, sigs, ops))
    print(f"\n报告：data/report/eval/{day}.md；累计：data/report/eval/summary.md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
