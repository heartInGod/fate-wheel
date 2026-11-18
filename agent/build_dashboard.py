#!/usr/bin/env python3
"""生成看板数据 dashboard.json + dashboard.js（供根目录 dashboard.html 加载）。

  python3 main.py dashboard            # 用 cache/snapshot_latest.json 聚合，写 data/derived/dashboard.json 与 dashboard.js
  python3 main.py dashboard fresh=true # 强制重新拉一次盘中 snapshot 后再聚合

输出：
- data/derived/dashboard.json：结构化数据本体（机器可读，便于复查/其他脚本消费）。
- data/derived/dashboard.js：同一份数据包装成 `window.FW_DASHBOARD_DATA = {...}`，
  由前端用 <script src> 加载。这样双击根目录 dashboard.html（file://）也能取到数据，
  因为 <script src> 不受 fetch/XHR 在 file:// 下的 CORS 限制，无需启动本地 HTTP 服务。
前端入口是根目录 dashboard.html（前端资源在 web/）：优先读 dashboard.js；通过 HTTP 访问时若 JS 缺失则回退 fetch dashboard.json。
页面中行情为外链 JSONP（同样是 script 标签），file:// 下也能实时拉取。
profiles/、data/announcements/、logs/、data/portfolio/thesis.json（定位前提）、cache/commodity.json（关联商品价格，盘前快照写入），日 K / 技术位 / 汇率读 cache/ 本地缓存（不联网；fresh=true 时才联网刷新），页面打开后再实时拉取；「长线 & 政策」页读 data/derived/core_auto.json
（core_value.py review 生成：分仓、目标价、行业景气 = 基础分 + 衰减政策分、个股建议总表）和最新一期
data/research/policy/*_政策信号报告.md。
"""
import csv
import datetime
import glob
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import signal_store as sst
from datasources import archive, commodity, quotes
from datasources import thesis as th
from datasources import watchlist as wl
from datasources._http import normalize
from datasources.holdings import ROOT, account, holdings
from datamul import data_path, data_dir
from datasources.snapshot import CACHE_DIR, snapshot

DATA_DIR = os.path.join(ROOT, "data", "derived")
OUTPUT = os.path.join(DATA_DIR, "dashboard.json")
OUTPUT_JS = os.path.join(DATA_DIR, "dashboard.js")
OPERATIONS_CSV = data_path("operations.csv")


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def load_snapshot(fresh=False):
    path = os.path.join(CACHE_DIR, "snapshot_latest.json")
    if not fresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    try:
        return snapshot("intraday")
    except Exception as e:  # noqa: BLE001
        return {"errors": [f"snapshot 失败: {e}"], "positions": []}


def load_operations():
    if not os.path.exists(OPERATIONS_CSV):
        return []
    with open(OPERATIONS_CSV, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]
    return sorted(rows, key=lambda r: (r.get("日期", ""), r.get("时间", "")), reverse=True)


_SEC = re.compile(r"^## +(\d{1,2}:\d{2})\s*(.*)$")


def load_logs():
    """logs/yyyyMMdd.md → 每天的原文 + 按 `## HH:MM 标题` 切分的段落；并抽取操作建议/提醒记录。"""
    days, alerts = [], []
    for path in sorted(glob.glob(os.path.join(ROOT, "logs", "*.md")), reverse=True):
        m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})\.md", os.path.basename(path))
        if not m:
            continue
        date = "-".join(m.groups())
        text = _read(path) or ""
        sections, cur = [], None
        for line in text.splitlines():
            h = _SEC.match(line)
            if h:
                cur = {"time": h.group(1), "title": h.group(2).strip(), "lines": []}
                sections.append(cur)
            elif cur is not None:
                cur["lines"].append(line)
        out_secs = []
        for s in sections:
            body = "\n".join(s["lines"]).strip()
            feishu = next((ln.split("：", 1)[-1].strip() for ln in s["lines"]
                           if re.match(r"^\s*- *飞书[：:]", ln)), "")
            conclusion = next((ln.split("：", 1)[-1].strip() for ln in s["lines"]
                               if re.match(r"^\s*- *结论[：:]", ln)), "")
            signals, in_adv = [], False
            for ln in s["lines"]:
                st = ln.strip()
                if re.match(r"^- *信号[：:]", st):
                    signals.append(st.split("：", 1)[-1].strip() if "：" in st else st[4:])
                    in_adv = False
                elif re.match(r"^- *建议[：:]", st):
                    rest = st.split("：", 1)[-1].strip()
                    if rest and not re.search(r"无操作建议|无建议", rest):
                        signals.append(rest)
                    in_adv = True
                elif in_adv and re.match(r"^\s{2,}- ", ln):
                    signals.append(st[2:].strip())
                elif re.match(r"^- ", st):
                    in_adv = False
            kind = ("premarket" if "盘前" in s["title"] else "close" if "收盘" in s["title"]
                    else "intraday")
            sec = {"time": s["time"], "title": s["title"], "kind": kind, "body": body,
                   "conclusion": conclusion, "feishu": feishu, "signals": signals}
            out_secs.append(sec)
            if signals:
                alerts.append({"date": date, "time": s["time"], "title": s["title"], "kind": kind,
                               "signals": signals, "feishu": feishu})
        days.append({"date": date, "markdown": text, "sections": out_secs})
    return days, alerts


def load_klines(codes, fresh=False):
    """日 K（近 120 根）。默认只读本地缓存 cache/kline/，不联网（快照 / 复查每次成功拉 K 线都会写缓存）；
    fresh=True 时联网拉取并写回缓存，失败退回缓存。页面打开后浏览器会再实时刷新一次 K 线。"""
    def conv(rows):
        return [[r["date"], r["open"], r["close"], r["high"], r["low"], r["volume"]] for r in rows][-120:]

    def one(c):
        if fresh:
            try:
                return c, conv(quotes.kline(c, count=120))
            except Exception:  # noqa: BLE001  （含熔断：腾讯被封时很快失败）
                pass
        return c, conv(quotes.cached_kline(c)[0])
    if not fresh:
        return dict(one(c) for c in codes)
    with ThreadPoolExecutor(max_workers=4) as ex:
        return dict(ex.map(one, codes))


FX_CACHE = os.path.join(CACHE_DIR, "hkd_cny.json")


def hkd_rate(fresh=False):
    """港币兑人民币。默认读缓存；fresh 或没有缓存时联网（腾讯 whHKDCNY），成功写回缓存。"""
    try:
        with open(FX_CACHE, encoding="utf-8") as f:
            cached = json.load(f).get("rate")
    except (OSError, ValueError):
        cached = None
    if cached and not fresh:
        return cached
    try:
        rate = float(quotes._fetch_tencent(["whHKDCNY"]).get("whHKDCNY")[3])
        with open(FX_CACHE, "w", encoding="utf-8") as f:
            json.dump({"rate": rate, "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f)
        return rate
    except Exception:  # noqa: BLE001
        return cached


def _cached_watch_tech(codes):
    """不联网的技术位：cache/watch_tech.json（不限日期），再缺的用本地日 K 缓存现算。"""
    try:
        with open(wl.TECH_CACHE, encoding="utf-8") as f:
            cache = json.load(f).get("tech") or {}
    except (OSError, ValueError):
        cache = {}
    today = datetime.date.today().strftime("%Y%m%d")
    out = {}
    for c in codes:
        if cache.get(c):
            out[c] = cache[c]
            continue
        try:
            out[c] = wl.tech_from_klines(quotes.cached_kline(c)[0], today)
        except Exception:  # noqa: BLE001
            pass
    return out


def load_watch(snap, held, fresh=False):
    """自选股：名单 + 快照行情/技术位 + 近 30 天公告 + 近 10 天自选信号。
    快照里没有技术位的：默认读本地缓存（不联网），fresh 时才联网补拉。"""
    items = [w for w in wl.watchlist() if w["code"] not in held]
    if not items:
        return []
    rows = {r["code"]: r for r in snap.get("watchlist") or []}
    miss = [w["code"] for w in items if not (rows.get(w["code"]) or {}).get("tech")]
    tech = {}
    if miss:
        try:
            tech, _ = wl.watch_tech(miss) if fresh else ({}, [])
        except Exception:  # noqa: BLE001
            tech = {}
        tech = {**_cached_watch_tech([c for c in miss if c not in tech]), **tech}
    codes = {w["code"] for w in items}
    sigs = {}
    for path in sorted(glob.glob(os.path.join(sst.SIG_DIR, "*.jsonl")))[-10:]:
        day = os.path.basename(path)[:8]
        for sg in sst.load_signals(day).values():
            if sg.get("code") in codes and sg.get("type") in sst.WATCH_TYPES:
                sigs.setdefault(sg["code"], []).append({
                    "date": day, "time": sg.get("time"), "label": sg.get("label"), "price": sg.get("price"),
                    "zone": sg.get("zone"), "qty": sg.get("qty"), "decision": sg.get("decision"),
                    "sent": sg.get("sent"), "reason": sg.get("reason"), "note": sg.get("decision_note")})
    out = []
    for w in items:
        c = w["code"]
        r = rows.get(c)
        out.append({**w, "key": archive.file_key(c), "tencent": normalize(c)["tencent"], "snap": r,
                    "tech": (r or {}).get("tech") or tech.get(c),
                    "announcements": archive.read_announcements(c, 30)[:8],
                    "signals": sorted(sigs.get(c, []), key=lambda x: (x["date"], x["time"] or ""), reverse=True)})
    return out


GROUP_LABEL = {"core": "长线", "trade": "交易", "watch": "待观察"}


def _checks(s, cfg):
    """长线准入条件逐项检查（口径同 core_value.screen / etf_value.judge）：[(条件, 实际值, 是否通过)]。"""
    def ok(v, f):
        return None if v is None else bool(f(v))
    if s.get("kind") == "etf":
        e = cfg.get("etf") or {}
        hist, amt, sc, fee = s.get("hist_years"), s.get("amount_wan"), s.get("scale_yi"), s.get("fee_pct")
        return [
            (f"估值历史 ≥ {e.get('hist_min_years')} 年", f"{hist} 年" if hist is not None else "-", ok(hist, lambda v: v >= e.get("hist_min_years", 5))),
            (f"PE 异常天数 ≤ {e.get('bad_share_max')}%", f"{s.get('bad_share')}%", ok(s.get("bad_share"), lambda v: v <= e.get("bad_share_max", 20))),
            (f"指数盈利年化 ≥ {e.get('earn_cagr_min')}%", f"{s.get('earn_cagr')}%（{s.get('earn_years')} 年）", ok(s.get("earn_cagr"), lambda v: v >= e.get("earn_cagr_min", 0))),
            (f"规模 ≥ {e.get('min_scale_yi')} 亿", f"{sc} 亿", ok(sc, lambda v: v >= e.get("min_scale_yi", 20))),
            (f"成交额 ≥ {e.get('min_amount_wan')} 万", f"{amt} 万", ok(amt, lambda v: v >= e.get("min_amount_wan", 3000))),
            (f"管理费 ≤ {e.get('fee_max_pct')}%", f"{fee}%", ok(fee, lambda v: v <= e.get("fee_max_pct", 0.6))),
        ]
    if "roe_last" not in s and "cap_yi" not in s:
        return []
    pe_max = s.get("pe_max_used") or cfg.get("pe_max")
    fin = any(k in (s.get("industry") or "") for k in ("银行", "证券", "保险", "多元金融"))
    r1, r0, ry = s.get("roe_last"), s.get("roe_prev"), s.get("roe_ytd_ann")
    f1 = lambda v: f"{v:.1f}%" if isinstance(v, (int, float)) else "-"   # noqa: E731
    return [
        (f"市值 ≥ {cfg.get('min_cap_yi')} 亿", f"{s.get('cap_yi')} 亿", ok(s.get("cap_yi"), lambda v: v >= cfg.get("min_cap_yi", 150))),
        (f"年报 ROE 前年 ≥ {cfg.get('roe_prev_min')}% / 去年 ≥ {cfg.get('roe_last_min')}%", f"{f1(r0)} / {f1(r1)}",
         None if r0 is None or r1 is None else r0 >= cfg.get("roe_prev_min", 10) and r1 >= cfg.get("roe_last_min", 12)),
        (f"今年 ROE 年化 ≥ {cfg.get('roe_ytd_min')}%", f1(ry), ok(ry, lambda v: v >= cfg.get("roe_ytd_min", 10))),
        ("归母净利同比 ≥ 0", f1(s.get("np_yoy")), ok(s.get("np_yoy"), lambda v: v >= 0)),
        ("营收同比 ≥ 0", f1(s.get("rev_yoy")), ok(s.get("rev_yoy"), lambda v: v >= 0)),
        (f"PE(TTM) 在 0~{pe_max}" + ("（高景气放宽）" if pe_max != cfg.get("pe_max") else ""), str(s.get("pe_ttm")),
         ok(s.get("pe_ttm"), lambda v: 0 < v <= pe_max)),
        (f"负债率 ≤ {cfg.get('debt_max')}%" + ("（金融业豁免）" if fin else ""), f1(s.get("debt_pct")),
         True if fin else ok(s.get("debt_pct"), lambda v: v <= cfg.get("debt_max", 70))),
        (f"机构覆盖 ≥ {cfg.get('min_orgs')} 家", f"{s.get('orgs')} 家" if s.get("orgs") is not None else "-",
         ok(s.get("orgs"), lambda v: v >= cfg.get("min_orgs", 3))),
    ]


def sleeve_detail(core):
    """每只持仓：类型、归属（长线 / 交易 / 待观察）、归属原因、长线条件逐项检查、复查提示。
    待观察 = 长线仓里归属待定的：触发逻辑复查，或本次没过长线条件、只因「上次已归长线仓」留着。"""
    cfg, stocks, out = core.get("params") or {}, core.get("stocks") or {}, {}
    for c, v in (core.get("sleeves") or {}).items():
        s = stocks.get(c) or {}
        why = v.get("why") or ""
        head, _, ind = why.partition("；行业：")
        review = [x for x in (s.get("review") or []) if x]
        pending = v.get("sleeve") == "core" and (bool(review) or "本次未" in head)
        if "force" in head:
            typ = "固定归属"
        elif s.get("kind") == "etf":
            typ = "ETF · " + (s.get("tracking") or s.get("val_index") or "")
        elif "港股" in head or "非个股" in head:
            typ = "港股 / 非个股（不适用财报估值）"
        else:
            typ = "A 股个股" + (" · " + s["industry"] if s.get("industry") else "")
        tier = {"hot": "高景气", "neutral": "中性", "cold": "低景气"}.get(s.get("industry_tier"))
        group = "watch" if pending else v.get("sleeve") or "trade"
        out[c] = {"sleeve": v.get("sleeve"), "group": group, "group_label": GROUP_LABEL.get(group, group),
                  "type": typ, "why": head, "industry_note": ind, "tier": tier,
                  "checks": [{"k": k, "v": val, "ok": p} for k, val, p in _checks(s, cfg)],
                  "review": review, "screen_ok": s.get("screen_ok"),
                  "zone": s.get("zone"), "target": s.get("target"), "upside_pct": s.get("upside_pct"),
                  "action": s.get("action"), "buy_block": s.get("buy_block") or s.get("entry_block"),
                  "sell_block": s.get("sell_block")}
    return out


def cand_detail(core, c, row):
    """候选区（复查建议表 who=候选）的一只：为什么在候选区、长线条件逐项、估值分区与系统动作。"""
    cfg, s = core.get("params") or {}, (core.get("stocks") or {}).get(c) or {}
    etf = s.get("kind") == "etf"
    pol = row.get("policy")
    return {"sleeve": None, "group": "cand", "group_label": "候选",
            "type": ("ETF · " + (s.get("tracking") or s.get("val_index") or "")) if etf
            else "A 股个股" + (" · " + s["industry"] if s.get("industry") else ""),
            "why": row.get("advice") or "", "industry_note": pol if pol and pol != "-" else "",
            "tier": {"hot": "高景气", "neutral": "中性", "cold": "低景气"}.get(s.get("industry_tier")),
            "checks": [{"k": k, "v": val, "ok": p} for k, val, p in _checks(s, cfg)],
            "review": [x for x in (s.get("review") or []) if x], "screen_ok": s.get("screen_ok"),
            "zone": s.get("zone"), "target": s.get("target"), "upside_pct": s.get("upside_pct"),
            "action": s.get("action"), "buy_block": s.get("buy_block") or s.get("entry_block"),
            "sell_block": s.get("sell_block"), "policy_pts": row.get("pp"), "industry_score": row.get("score")}


def load_cands(core, watch, klines):
    """候选区股票 → 与持仓同结构的个股详情对象（行情取自选快照，资料/公告/K 线同持仓）。"""
    if not core:
        return []
    wmap = {re.sub(r"\D", "", w["code"]): w for w in watch}
    out = []
    for r in core.get("advice") or []:
        if r.get("who") != "候选":
            continue
        w = wmap.get(re.sub(r"\D", "", r["code"])) or {}
        c = w.get("code") or r["code"]
        snap = dict(w.get("snap") or {})
        if not snap.get("tech") and w.get("tech"):
            snap["tech"] = w["tech"]
        key = archive.file_key(c)
        out.append({"code": c, "name": r.get("name") or w.get("name"), "key": key, "tencent": normalize(c)["tencent"],
                    "cand": True, "type": "自选 · 候选区", "currency": "CNY", "shares": None, "available": None,
                    "cost": None, "stance": w.get("focus") or "", "source": w.get("source"), "added": w.get("added"),
                    "industry": w.get("industry") or r.get("industry"), "snap": snap or None, "premium": None,
                    "profile_md": _read(os.path.join(archive.PROFILE_DIR, f"{key}.md")),
                    "announcements": archive.read_announcements(c, 90), "kline": klines.get(c, [])})
    return out


def _trade_ctx():
    """交易理由用：每只标的 ATR20%（rules_auto.json，calibrate.py 生成）+ 交易仓规则（rules_params.json sleeves.trade）。"""
    ctx = {"atr": {}, "median_atr": None, "min_move_pct": 3.0, "hold_days": 5}
    try:
        with open(data_path("rules_auto.json"), encoding="utf-8") as f:
            a = json.load(f)
        ctx["atr"] = {re.sub(r"\D", "", k).lstrip("0"): v for k, v in (a.get("atr") or {}).items()}
        ctx["median_atr"] = a.get("median_atr")
    except (OSError, ValueError):
        pass
    try:
        with open(data_path("rules_params.json"), encoding="utf-8") as f:
            t = (json.load(f).get("sleeves") or {}).get("trade") or {}
        ctx["min_move_pct"] = t.get("min_move_pct", 3.0)
        ctx["hold_days"] = t.get("hold_days", 5)
    except (OSError, ValueError):
        pass
    return ctx


def _safe_detail(core):
    try:
        out, ctx = sleeve_detail(core), _trade_ctx()
        stocks = core.get("stocks") or {}
        for c, d in out.items():
            s = stocks.get(c) or {}
            if s.get("kind") == "etf":
                e = ((core.get("policy") or {}).get("etf_industry") or {}).get(c) or {}
                s = dict(s, policy_pts=e.get("policy_pts"), industry_score=e.get("score"), industry_tier=e.get("tier"))
                d["tier"] = d.get("tier") or {"hot": "高景气", "neutral": "中性", "cold": "低景气"}.get(e.get("tier"))
                d["etf_industry"] = e.get("industry") or e.get("name")
            d.update(atr=ctx["atr"].get(re.sub(r"\D", "", c).lstrip("0")), median_atr=ctx["median_atr"],
                     min_move_pct=ctx["min_move_pct"], hold_days=ctx["hold_days"],
                     policy_pts=s.get("policy_pts"), industry_score=s.get("industry_score"))
        return out
    except Exception as e:  # noqa: BLE001
        core.setdefault("errors", []).append(f"持仓归属明细生成失败：{e}")
        return {}


def load_core():
    """长线仓复查结果 + 政策信号（只读 core_auto.json，不联网）。没有文件时返回 None，页面显示提示。"""
    path = data_path("core_auto.json")
    try:
        with open(path, encoding="utf-8") as f:
            core = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        import core_value as cv
        rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in cv.advice_rows(core)]
        etfi = (core.get("policy") or {}).get("etf_industry") or {}
        for r in rows:   # 数值字段：排序 / 筛选 / 实时重算空间用
            s = (core.get("stocks") or {}).get(r["code"]) or {}
            e = etfi.get(r["code"]) or {}
            r.update(held=bool(s.get("held")) or r["who"].startswith("持仓"), kind=s.get("kind"), zone_key=s.get("zone"),
                     pp=(e.get("policy_pts") if s.get("kind") == "etf" else s.get("policy_pts")) or 0,
                     score=e.get("score") if s.get("kind") == "etf" else s.get("industry_score"),
                     tier_key=e.get("tier") if s.get("kind") == "etf" else s.get("industry_tier"),
                     tier_base_key=e.get("tier_base") if s.get("kind") == "etf" else s.get("industry_tier_base"),
                     qty=s.get("qty"), cost=s.get("cost"))
        sleeve = {c: r.get("sleeve") for c, r in (core.get("stocks") or {}).items()}
        sleeve.update({c: v.get("sleeve") for c, v in (core.get("sleeves") or {}).items()})
    except Exception as e:  # noqa: BLE001
        rows, sleeve = [], {}
        core.setdefault("errors", []).append(f"个股建议表生成失败：{e}")
    detail = _safe_detail(core)
    for r in rows:
        if r.get("who") == "候选" and r["code"] not in detail:
            try:
                detail[r["code"]] = cand_detail(core, r["code"], r)
            except Exception as e:  # noqa: BLE001
                core.setdefault("errors", []).append(f"候选 {r['code']} 明细生成失败：{e}")
    pol = core.get("policy") or {}
    involved = set((core.get("industry") or {}).get("involved") or [])
    inds = [dict(v, name=k) for k, v in (pol.get("industries") or {}).items() if v.get("policy_pts") or k in involved]
    inds.sort(key=lambda x: -(x.get("policy_pts") or 0))
    reports = sorted(glob.glob(data_dir("research", "policy", "*_政策信号报告.md")))
    rep = {"name": os.path.relpath(reports[-1], ROOT), "md": _read(reports[-1])} if reports else None
    return {"asof": core.get("asof"), "generated_at": core.get("generated_at"), "summary": core.get("summary") or {},
            "advice": rows, "sleeve": sleeve, "detail": detail, "errors": (core.get("errors") or [])[:10],
            "policy": {"enabled": pol.get("enabled"), "asof": pol.get("asof"), "active": pol.get("active") or [],
                       "industries": inds, "stocks": pol.get("stocks") or {}, "params": pol.get("params") or {}},
            "industry_span": (core.get("industry") or {}).get("span"), "report": rep}


def st_params():
    import signal_store as st
    return st.load_params()


def build(fresh=False):
    hs = holdings()
    codes = [h["code"] for h in hs]
    snap = load_snapshot(fresh)
    pos_map = {p["code"]: p for p in snap.get("positions") or []}
    prem = {e["code"]: e for e in snap.get("etf_premium") or [] if isinstance(e, dict) and e.get("code")}
    logs, alerts = load_logs()
    core = load_core()
    watch = load_watch(snap, set(codes), fresh)
    wmap = {re.sub(r"\D", "", w["code"]): w["code"] for w in watch}
    cand_codes = [wmap.get(re.sub(r"\D", "", r["code"]), r["code"]) for r in (core or {}).get("advice") or []
                  if r.get("who") == "候选"]
    klines = load_klines(codes + [c for c in cand_codes if c not in codes], fresh)
    ths = th.summaries(codes)
    cmd = commodity.load_cache()
    stocks = []
    for h in hs:
        c, key = h["code"], archive.file_key(h["code"])
        stocks.append({
            **h, "key": key, "tencent": normalize(c)["tencent"],
            "snap": pos_map.get(c), "premium": prem.get(c),
            "profile_md": _read(os.path.join(archive.PROFILE_DIR, f"{key}.md")),
            "announcements": archive.read_announcements(c, 90),
            "kline": klines.get(c, []), "thesis": ths.get(c),
            "commodity": [cmd[i] for i in (ths.get(c) or {}).get("commodities") or [] if i in cmd],
        })
    try:
        import signals_engine as se
        pulse = se.classify_pulse(snap, st_params())
    except Exception as e:  # noqa: BLE001
        pulse = {"level": "none", "label": f"计算失败：{e}", "reasons": []}
    if snap.get("market_pulse"):
        pulse["etfs"] = [{k: e.get(k) for k in ("code", "name", "group", "vol_ratio", "pct", "ret_15m")}
                         for e in snap["market_pulse"].get("etfs") or []]
    pulse["shares"] = load_shares(snap)
    pulse["news"] = [{"time": n.get("time"), "text": n.get("text"), "keywords": n.get("keywords")}
                     for n in (snap.get("policy_news") or [])[:8]]
    data = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "account": account(), "hkd_cny": hkd_rate(fresh) or 0.85,
        "snapshot_at": snap.get("generated_at"), "snapshot_mode": snap.get("mode"),
        "snapshot_errors": snap.get("errors") or [], "market": snap.get("market"),
        "indices": snap.get("indices") or [], "connect_flow": snap.get("connect_flow"),
        "sector_in": snap.get("sector_flow_in") or [], "sector_out": snap.get("sector_flow_out") or [],
        "stocks": stocks, "cands": load_cands(core, watch, klines), "pulse": pulse, "watch": watch, "logs": logs, "alerts": alerts, "operations": load_operations(),
        "core": core,
    }
    # 同时写 JSON（机器可读）与 JS（前端 <script src> 直接加载，file:// 下无需 HTTP 服务）。
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = OUTPUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, default=str)
    os.replace(tmp, OUTPUT)
    # JS 包装：把 JSON 字面量赋给全局变量。< 转义为 \u003c，避免日志/资料正文里出现
    # </script> 之类文本破坏脚本（即使当前是外链文件也保一手）；U+2028/2029 同样转义。
    payload = json.dumps(data, ensure_ascii=False, default=str)
    payload = (payload.replace("<", "\\u003c")
                      .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    tmp_js = OUTPUT_JS + ".tmp"
    with open(tmp_js, "w", encoding="utf-8") as f:
        f.write("/* 由 main.py dashboard 生成，勿手工编辑；含持仓隐私，已 gitignore。 */\n")
        f.write("window.FW_DASHBOARD_DATA = " + payload + ";\n")
    os.replace(tmp_js, OUTPUT_JS)
    return OUTPUT, data


def load_shares(snap):
    """宽基 ETF 份额（上一交易日净申购）。盘中快照没有，取最近一次盘前快照（4 天内）；只展示，不改变盘中等级。"""
    sh = (snap.get("market_pulse") or {}).get("shares")
    if not sh:
        try:
            with open(os.path.join(CACHE_DIR, "snapshot_premarket.json"), encoding="utf-8") as f:
                pre = json.load(f)
            age = datetime.datetime.now() - datetime.datetime.strptime(str(pre.get("generated_at"))[:10], "%Y-%m-%d")
            if age.days <= 4:
                sh = (pre.get("market_pulse") or {}).get("shares")
        except (OSError, ValueError):
            sh = None
    if not sh or not sh.get("groups"):
        return None
    try:
        import signals_engine as se
        r = se.classify_pulse({"market_pulse": {"shares": sh}}, st_params())
    except Exception:  # noqa: BLE001
        return None
    return {"date": sh.get("date"), "level": r.get("level"), "label": r.get("label"),
            "groups": r.get("shares_groups") or {}, "hs300_yi": r.get("shares_hs300_yi"),
            "cum": r.get("shares_hs300_cum_pct"), "days": r.get("shares_days"),
            "history": sh.get("history") or []}


if __name__ == "__main__":
    fresh = any(a.lower() in ("fresh=true", "--fresh") for a in sys.argv[1:])
    path, d = build(fresh)
    print(f"已生成 {path}（持仓 {len(d['stocks'])} 只，自选 {len(d['watch'])} 只，日志 {len(d['logs'])} 天，"
          f"提醒 {len(d['alerts'])} 条，成交 {len(d['operations'])} 条，长线建议 {len((d.get('core') or {}).get('advice') or [])} 行，"
          f"行情快照 {d['snapshot_at']}）")
