#!/usr/bin/env python3
"""盘前一次性摘要：把 snapshot + signals_engine + thesis + 经验要点压成一份约 10 KB 的 Markdown。

背景
----
09:00 盘前任务原先的做法是：先跑 snapshot（147 KB JSON）、再跑 signals_engine（34 KB JSON），
然后由大模型用十几轮 python3 -c 小片段反复挖字段。每轮都是一次完整模型调用往返，
再加上这些数据把上下文撑到触发压缩，09:00 的任务经常要跑 15~20 分钟才写出日志，
9:30 盘中任务就已经读不到早报了。

本脚本用一次进程内调用产出写好日志所需的全部结构化摘要，正文只有约 10 KB，
大模型只需一次工具调用即可进入判断环节。

压缩原则（相对全量 JSON）
------------------------
- 持仓 11 只：逐只保留，是模型判断主体；支撑/压力只留现价最近两档并去掉同价重复。
- 长线仓候选 44 只：绝大多数是同一句「买入区但长线仓已满」，按动作分组给计数，
  只单独点出有可执行动作的那只 + 空间前 6 名（留空位时用）。
- 自选股 52 只：只展开贴近支撑（距支撑 <2%）的，其余压成一行名字。
- 用 --full 可退回全量列表。

用法：
  python3 main.py premarket                 # 默认读 cache/snapshot_premarket.json
  python3 main.py premarket --snapshot x.json
  python3 main.py premarket --json          # 输出结构化 JSON 而不是 Markdown
  python3 main.py premarket --full          # 候选股/自选股全部逐条展开（体积约 33 KB）
  python3 main.py premarket --full-news     # 新闻摘要放宽到 3 条/只（默认 2 条）

只读：不写任何文件、不记录信号。真正的结论仍由大模型判断后写入 data/report/。
"""
import argparse
import datetime
import json
import os
import sys

import signals_engine as se
from datasources import thesis as T

from paths import ROOT
from datamul import data_path
SNAP_PRE = os.path.join(ROOT, "cache", "snapshot_premarket.json")
THESIS = data_path("thesis.json")

NEWS_N = 2
NEWS_CHARS = 110
TOUCH_GAP = -2.0          # 距支撑在此百分比以内算「贴近支撑」
CAND_TOP = 6              # 候选股默认只列空间前 N 名
WATCH_FOCUS = 34          # 自选股 focus（ROE/净利/PE/社保）保留字数

_ROLE = {"t": "做T", "add": "可加仓", "hold": "持有", "watch": "观察"}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _num(v, nd=2):
    v = _f(v)
    if v is None:
        return "-"
    return f"{v:,.{nd}f}".rstrip("0").rstrip(".") if nd else f"{v:,.0f}"


def _pct(v, nd=2):
    v = _f(v)
    return "-" if v is None else f"{v:+.{nd}f}%"


def _gap(v, price):
    v, price = _f(v), _f(price)
    return None if not v or not price else (v / price - 1) * 100


def _lvl(pairs, price):
    """支撑/压力位写成 价位(距现价%) 的形式，保留全部档位（--full 用）。"""
    out = []
    for name, val in pairs or []:
        v = _f(val)
        if not v:
            continue
        out.append(f"{name} {_num(v, 3)}({_pct(_gap(v, price), 1)})")
    return "、".join(out) or "-"


def _lvl_near(pairs, price, side, keep=2):
    """只留现价最近的两档，去掉同价重复：支撑取下方最高的，压力取上方最低的。"""
    price = _f(price)
    if not price:
        return "-"
    seen = {}
    for _name, val in pairs or []:
        v = _f(val)
        if not v:
            continue
        if side == "sup" and v > price * 1.001:
            continue
        if side == "res" and v < price * 0.999:
            continue
        seen.setdefault(round(v, 3), True)
    vals = sorted(seen, reverse=(side == "sup"))[:keep]
    if not vals:
        return "-"
    return "、".join(f"{v:g}({_gap(v, price):+.1f}%)" for v in vals)


def _clip(s, n=NEWS_CHARS):
    s = (s or "").strip().replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


def _clip_parts(s, n):
    """按 ｜ 分段截断，宁可少一段也不把「目标 37…」这种字段切半。"""
    parts = [x for x in (s or "").split("｜") if x.strip()]
    out, ln = [], 0
    for x in parts:
        if out and ln + len(x) + 1 > n:
            break
        out.append(x)
        ln += len(x) + 1
    txt = "｜".join(out)
    return txt or _clip(s, n)


def _roles(rs):
    return "/".join(_ROLE.get(r, str(r)) for r in rs or []) or "-"


def load_thesis():
    try:
        with open(THESIS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"items": {}, "stale_days": {}}


def thesis_lines(th):
    """前提状态：今日须核实的 + 仍在有效期的。

    口径直接复用 datasources.thesis.summary（按 checked 日期 + stale_days 判定 stale、
    已归档的前提不列），避免在本脚本里重算一套判定标准。
    """
    items = th.get("items") or {}
    today = datetime.date.today()
    need, have = [], []
    for code, it in items.items():
        name = it.get("name") or code
        s = T.summary(code, th, today)
        for p in s.get("premises") or []:
            age = "从未核实" if p.get("age_days") is None else f"{p['age_days']}天前核实"
            row = (f"{name} {code} {p.get('id')} [{p.get('kind')}] {p.get('text')} | "
                   f"{p.get('status')} | {age}｜时限 {p.get('ttl_days')}天 | 支{p.get('supports')}")
            (need if p.get("stale") else have).append(row)
    return need, have


def _core_tag(it):
    """长线仓决策压成一行尾巴：目标/空间 + 保本价 + 动作 + 约束。逐只持仓处复用，避免两段重复。"""
    out = ""
    if it.get("target"):
        out += f" | 目标 {_num(it.get('target'))}(空间 {_pct(it.get('upside_pct'), 0)})"
    if it.get("breakeven"):
        out += f" 保本 {_num(it.get('breakeven'))}"
    qty = it.get("review_qty")
    act = it.get("review_action") or ""
    if act and act != "无":
        out += f" | 动作 {_clip(act, 40)}" + (f" {_num(qty, 0)} 股" if qty else "")
    for key in ("cost_block", "entry_block"):
        if it.get(key):
            out += f" | {_clip(it[key], 60)}"
    if it.get("review"):
        out += f" | 复查：{_clip('；'.join(map(str, it['review'])), 50)}"
    return out


def build(snap, res, th, news_n=NEWS_N, full=False):
    mk = snap.get("market") or {}
    acc = snap.get("account") or {}
    auto = res.get("auto") or {}
    pf = res.get("portfolio") or {}
    core = res.get("core") or {}
    pulse = res.get("market_pulse") or {}
    lines = []
    A = lines.append

    A("## 市场与账户")
    A(f"- 取数时间 {snap.get('generated_at')} | A股开市 {mk.get('a_open_today')}（最近行情 {mk.get('a_last_quote_date')}）"
      f" | 港股开市 {mk.get('hk_open_today')}（{mk.get('hk_last_quote_date')}）")
    idx = "、".join(f"{i.get('name')} {_num(i.get('price'))}({_pct(i.get('pct'))})" for i in snap.get("indices") or [])
    A(f"- 指数：{idx or '-'}")
    A(f"- 账户：总资产 {_num(acc.get('total_assets'))} | 市值 {_num(acc.get('market_value'))} | "
      f"可用 {_num(acc.get('cash_available'))} | 浮盈亏 {_num(acc.get('float_pnl'))}（{acc.get('updated')}）")
    rd = auto.get("regime_detail") or {}
    A(f"- 市场状态：{auto.get('regime_name')}（收 {_num(rd.get('close'))} / MA20 {_num(rd.get('ma20'))} / 斜率 {_num(rd.get('ma20_slope5'))}）"
      f" | 动态阈值 {'生效' if auto.get('enabled') else '未生效'}（校准 {auto.get('calibrated_at')}）")
    A("")
    A("## 两仓与预算")
    A(f"- {pf.get('note') or '-'}")
    A(f"- 长线仓市值 {_num((core.get('summary') or {}).get('core_value'))} / 预算 {_num((core.get('summary') or {}).get('core_budget'))}"
      f" | 可买空间 cash_room {_num(res.get('cash_room_yuan'), 0)}")
    A("")
    A("## 大盘脉搏（上一交易日）")
    A(f"- {pulse.get('label')}：{'；'.join(pulse.get('reasons') or []) or '-'}")
    pn = pulse.get("policy_news") or snap.get("policy_news") or []
    A(f"- 政策/国家队快讯 {len(pn)} 条" + (f"：{_clip(pn[0] if isinstance(pn[0], str) else pn[0].get('text'), 140)}" if pn else ""))
    A("")
    # 大盘资金面：读 snapshot 里的 market_flow，用 core/market_regime 分档
    from core import market_regime as _mr
    mf = snap.get("market_flow") or {}
    reg = _mr.classify(mf)
    A("## 大盘资金面")
    A(f"- 档位：**{reg.get('label')}**（{reg.get('regime')}）")
    for e in reg.get("evidence") or []:
        A(f"  - {e}")
    for c in reg.get("crosses") or []:
        A(f"  - 交叉验证：{c}")
    ext = mf.get("external") or {}
    es = "、".join(f"{k}={v}" for k, v in (("美元离岸人民币", ext.get("usdcnh")),
                   ("伦敦金", ext.get("gold")), ("WTI原油", ext.get("oil")),
                   ("纳指100", ext.get("nasdaq100"))) if v is not None)
    if es:
        A(f"  - 外盘：{es}")
    if reg.get("gate"):
        A(f"- 闸门提醒：{reg['gate']}")
    if mf.get("errors"):
        A(f"  - 数据缺失 {len(mf['errors'])} 项（主力资金20日累计等缺口用当日 WebSearch 补，勿臆造）")
    A("")

    # ---------- 长线仓 ----------
    cs = core.get("summary") or {}
    A("## 长线仓（core 分区）")
    A(f"- 市值 {_num(cs.get('core_value'), 0)} / 预算 {_num(cs.get('core_budget'), 0)}"
      f" | 持仓 {cs.get('n_core')}/{cs.get('max_positions')} 只"
      f" | 剩余额 {_num(cs.get('core_room_left'), 0)} | 现金 {_num(cs.get('cash'), 0)}")
    items = core.get("items") or []
    # 已持仓的决策（目标/动作/约束）并入下面「逐只（持仓）」段，这里只处理候选股，避免两段重复。
    cand = [i for i in items if i.get("sleeve") != "core"]
    if cand:
        cand_sorted = sorted(cand, key=lambda x: -(x.get("upside_pct") or 0))
        grp = {}
        for c in cand:
            grp.setdefault(c.get("review_action") or "无动作", []).append(c)
        A(f"- 候选股 {len(cand)} 只：" + "；".join(
            f"{len(v)} 只「{k}」" for k, v in sorted(grp.items(), key=lambda kv: -len(kv[1]))))
        # 有可执行动作的单独点出（与「已满」口径冲突时要让人看见）
        for c in cand_sorted:
            act = c.get("review_action") or ""
            if act and "已满" not in act and act != "无":
                A(f"  ⚠ {c.get('name')} {c.get('code')} 现 {_num(c.get('price'))} → "
                  f"目标 {_num(c.get('target'))}({_pct(c.get('upside_pct'), 1)}) | 动作 {_clip(act, 40)}"
                  + (f" {_num(c.get('review_qty'), 0)} 股" if c.get("review_qty") else "")
                  + "（与「长线仓已满」口径不一致，需人工确认）")
        if full:
            A("- 候选股全量：")
            for c in cand_sorted:
                A(f"  - {c.get('name')} {c.get('code')} {c.get('zone_name') or ''}"
                  f" | 现 {_num(c.get('price'))} → 目标 {_num(c.get('target'))}({_pct(c.get('upside_pct'), 1)})"
                  f" | PE {_num(c.get('pe_ttm'))}/{_num(c.get('pe_fair'))} | {c.get('industry')}"
                  f" | {_clip(c.get('review_action'), 30)}")
        else:
            top = "、".join(
                f"{c.get('name')} {c.get('code')} {_pct(c.get('upside_pct'), 0)}" for c in cand_sorted[:CAND_TOP])
            A(f"- 候选空间前 {min(CAND_TOP, len(cand_sorted))}（留出空位时按此顺序）：{top}")
    A("")

    A("## 交易仓在途")
    ot = res.get("open_trades") or []
    for t in ot:
        # 加仓单（type=add）没有 buy_price，用现价代替，避免出现「- →」空值。
        entry = _num(t.get("buy_price")) if t.get("buy_price") else f"现 {_num(t.get('price'))}"
        A(f"- {t.get('name')} {t.get('code')} | {entry} → 止盈 {_num(t.get('tp_price'))}"
          f" | 第 {t.get('held_days')}/{t.get('hold_days')} 天 | {t.get('action')}"
          + (f" | {_clip(t.get('note'), 40)}" if t.get("note") else ""))
    if not ot:
        A("- 无")
    A("")

    # ---------- 逐只持仓 ----------
    A("## 逐只（持仓）")
    lv = {x.get("code"): x for x in res.get("levels") or []}
    ct = {}
    for it in items:
        if it.get("sleeve") == "core":
            ct[str(it.get("code"))] = it
    for p in snap.get("positions") or []:
        code = str(p.get("code"))
        price = _f(p.get("price"))
        L = lv.get(code) or {}
        if price is None:
            A(f"- {p.get('name')} {code} | 无报价")
            continue
        A(f"- {p.get('name')} {code} | 收 {_num(price, 3)}({_pct(p.get('pct'))}) | {L.get('sleeve') or '-'}"
          f" | 仓位 {_num(p.get('market_value'), 0)} 可卖 {_num(p.get('available'), 0)}"
          + (f" | 成本 {_num(p.get('cost'), 3)} 浮盈亏 {_pct(p.get('pnl_pct'), 1)}"
             if p.get("cost") else "")
          + f" | 角色 {_roles(L.get('roles'))}"
          + (f" | 溢价 {_num(p.get('premium_pct'))}%"
             + (f"（上限 {_num(L.get('premium_max'))}%）" if L.get("premium_max") is not None else "")
             if p.get("premium_pct") is not None else "")
          + (_core_tag(ct[code]) if code in ct else ""))
        th_p = p.get("thesis") or {}
        if th_p.get("stale"):
            A("    ⏰ 待核实前提：" + "；".join(str(s.get("id")) + " " + str(s.get("text")) for s in th_p["stale"]))
        A(f"    支撑 {_lvl_near(L.get('supports'), price, 'sup')}"
          f" ｜ 压力 {_lvl_near(L.get('resistances'), price, 'res')}"
          + (f" ｜ 阈值 做T {_num((L.get('thresholds') or {}).get('做T最小振幅%'))}%"
             f" 加仓 {_num((L.get('thresholds') or {}).get('加仓回升%'))}%"
             f" 减仓 {_num((L.get('thresholds') or {}).get('减仓回升%'))}%"
             f" ｜ ATR {_num(L.get('atr20_pct'))}%" if L.get("thresholds") else ""))
        ann = p.get("announcements_new") or []
        if ann:
            A(f"    📢 新公告：{'；'.join(_clip(a.get('title') if isinstance(a, dict) else a, 60) for a in ann[:3])}")
        news = p.get("news") or []
        if news:
            A(f"    📰 {'；'.join(_clip(n.get('title'), 44) for n in news[:news_n])}")
        if p.get("stance"):
            A(f"    定位：{_clip(p.get('stance'), 150)}")
    A("")

    # ---------- 自选股 ----------
    wl = res.get("watch_levels") or []
    near, far = [], []
    for w in wl:
        sup = w.get("supports") or []
        g = _gap(sup[0][1], w.get("price")) if sup else None
        (near if (g is not None and g > TOUCH_GAP) else far).append((g if g is not None else -999.0, w))
    near.sort(key=lambda x: -x[0])
    A(f"## 自选股 {len(wl)} 只（关注点，非持仓）")
    if not wl:
        A("- 无")
    else:
        A(f"- 贴近支撑（距最近支撑 >{TOUCH_GAP:.0f}%）{len(near)} 只：")
        for g, w in near:
            price = _f(w.get("price"))
            ann = w.get("announcements_new") or []
            A(f"  - {w.get('name')} {w.get('code')} {_num(price, 3)}"
              f" | 支撑 {_lvl_near(w.get('supports'), price, 'sup', 1)}"
              f" | 5日 {_pct(w.get('chg5_pct'), 1)}"
              + (f" | 📢 {'；'.join(_clip(a.get('title') if isinstance(a, dict) else a, 36) for a in ann[:2])}" if ann else "")
              + (f" | {_clip_parts(w.get('focus'), WATCH_FOCUS)}" if w.get("focus") else ""))
        rest = [w for _g, w in far]
        if rest:
            if full:
                A(f"- 其余 {len(rest)} 只（距支撑 >2%，仅列出）：")
                for w in rest:
                    price = _f(w.get("price"))
                    A(f"  - {w.get('name')} {w.get('code')} {_num(price, 3)}"
                      f" | 支撑 {_lvl(w.get('supports'), price)} | 5日 {_pct(w.get('chg5_pct'), 1)}"
                      + (f" | {_clip(w.get('focus'), 50)}" if w.get("focus") else ""))
            else:
                A(f"- 其余 {len(rest)} 只（距支撑 >2%，未展开）：" + "、".join(
                    f"{w.get('name')} {w.get('code')}(5日 {_pct(w.get('chg5_pct'), 1)})" for w in rest))
    A("")

    need, have = thesis_lines(th)
    A("## 定位前提")
    A("- 今日须核实（已过期/从未核实）：")
    A("  " + ("\n  ".join(f"- {x}" for x in need) if need else "- 无，前提均在有效期内"))
    A("- 仍在有效期（可直接引用「成立」的作为理由）：")
    A("  " + ("\n  ".join(f"- {x}" for x in have) if have else "- 无"))
    errs = snap.get("errors") or []
    if errs:
        A("")
        A(f"## 数据缺失\n- {errs}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=SNAP_PRE)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--full-news", action="store_true")
    args = ap.parse_args()
    with open(args.snapshot, encoding="utf-8") as f:
        snap = json.load(f)
    params = se.st.load_params()
    res = se.premarket_levels(snap, params)
    if args.json:
        print(json.dumps({"brief_input": res, "snapshot_keys": sorted(snap.keys())}, ensure_ascii=False, indent=1, default=str))
        return 0
    print(build(snap, res, load_thesis(), news_n=3 if args.full_news else NEWS_N, full=args.full))
    return 0


if __name__ == "__main__":
    sys.exit(main())
