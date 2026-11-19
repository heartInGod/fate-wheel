"""汇总快照：一次性拉取盘前 / 盘中分析需要的全部数据，单项失败不影响整体。"""
import datetime
import json
import os
from concurrent.futures import ThreadPoolExecutor

from . import archive, capital, commodity, fund, market_pulse, news, quotes
from . import thesis as th
from . import watchlist as wl
from .holdings import ROOT, account, holdings, merge_position

CACHE_DIR = os.path.join(ROOT, "cache")


def _safe(errors, label, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        errors.append(f"{label}: {e}")
        return None


def snapshot(mode="intraday"):
    """mode=premarket：盘前全量（行情+技术位+近5日资金+两融+近3日公告+个股新闻+快讯），并刷新 profiles/ 资料档案；
    mode=intraday：盘中轻量（行情+技术位+实时资金流+板块资金+当日公告）。
    watchlist 为自选股（watchlist.csv）：行情一次批量拉取 + 技术位按天缓存；盘前另同步其公告（announcements_new）
    并把超过 7 天的资料档案刷新一次；盘中只看行情，不拉自选股公告。
    两种模式都会同步 data/announcements/ 公告档案，announcements_new 为上次同步后新收录的公告。
    market_pulse 为宽基 ETF 同时刻量能（识别国家队护盘迹象，盘前即上一交易日全天）；
    盘前另有 market_pulse.shares：沪市宽基 ETF 份额日变化（净申购，收盘后公布，近 5 个交易日）。
    positions[].thesis 为定位前提（thesis.json：premises 带 status/age_days/stale，stale 列出需核实的前提 id，
    invalid 为存疑/失效的前提，missing 表示没写前提，proposal 为待张小凡确认的定位调整建议）。
    positions[].commodity 为该持仓关联的商品 / 行业价格（thesis.json 的 commodities，见 commodity.py）：
    最新值、日期、1 日 / 1 周 / 1 月变化 %、近 30 天高低、最近 6 个点；盘前联网拉取，盘中读 cache/commodity.json（不联网）。
    policy_news 为政策 / 国家队相关快讯（盘中取近 35 分钟，盘前取上一交易日 15:00 以来）。"""
    errors = []
    hs = holdings()
    codes = [h["code"] for h in hs]
    by_code = {h["code"]: h for h in hs}
    out = {"mode": mode, "market": _safe(errors, "market_status", quotes.market_status),
           "account": account(), "indices": _safe(errors, "index_quotes", quotes.index_quotes)}

    qs = _safe(errors, "realtime_quotes", quotes.realtime_quotes, codes) or []
    positions = [merge_position(q, by_code.get(q["code"], {})) for q in qs]

    flows = {}
    if mode == "intraday":
        for f in _safe(errors, "fund_flow_realtime", capital.fund_flow_realtime, codes) or []:
            flows[f["code"]] = f

    def per_stock(h):
        c, e = h["code"], []
        item = {"tech": _safe(e, f"tech_levels {c}", quotes.tech_levels, c)}
        if mode == "premarket":
            item["fund_flow_5d"] = _safe(e, f"fund_flow_daily {c}", capital.fund_flow_daily, c, 5)
            if h["market"] != "hk":
                item["margin_3d"] = _safe(e, f"margin {c}", capital.margin, c, 3)
            item["news"] = _safe(e, f"stock_news {c}", news.stock_news, h["name"] or c, 5)
        return c, item, e

    ths = _safe(errors, "thesis", th.summaries, codes) or {}
    cids = sorted({i for t in ths.values() for i in (t or {}).get("commodities") or []})
    with ThreadPoolExecutor(max_workers=1) as cex:
        cfut = cex.submit(commodity.prices, cids) if (cids and mode == "premarket") else None
        with ThreadPoolExecutor(max_workers=6) as ex:
            details = list(ex.map(per_stock, hs))
        if cfut:
            crow = _safe(errors, "commodity", cfut.result) or []
        else:
            cache = commodity.load_cache()
            crow = [cache[i] for i in cids if i in cache]
    cmap = {r["id"]: r for r in crow}
    errors.extend(f"commodity {r['id']}: {r['error']}" for r in crow if r.get("error"))
    detail_map = {}
    for c, item, e in details:
        detail_map[c] = item
        errors.extend(e)

    for r in _safe(errors, "sync_announcements", archive.sync_announcements, codes, 3 if mode == "premarket" else 2) or []:
        d = detail_map.setdefault(r["code"], {})
        if r.get("status") == "error":
            errors.append(f"announcements {r['code']}: {r['error']}（已用本地档案）")
        d["announcements_new"] = r.get("new", [])
        if mode == "premarket":
            d["announcements_3d"] = archive.read_announcements(r["code"], 3)
        else:
            d["announcements_today"] = r.get("today", [])
    if mode == "premarket":
        prof = _safe(errors, "sync_profiles", archive.sync_profiles, codes) or []
        errors.extend(f"profile {r['code']}: {r['error']}" for r in prof if r["status"] == "error")
        out["profiles"] = {"dir": "profiles/", "updated": sum(r["status"] == "updated" for r in prof)}
    for p in positions:
        p.update(detail_map.get(p["code"], {}))
        p["thesis"] = ths.get(p["code"])
        links = (p["thesis"] or {}).get("commodities") or []
        if links:
            p["commodity"] = [cmap[i] for i in links if i in cmap]
        if p["code"] in flows:
            p["fund_flow_today"] = {k: v for k, v in flows[p["code"]].items() if k.endswith("net") or k.endswith("pct") or k == "source"}
    out["positions"] = positions
    out["watchlist"] = _watch(errors, mode, set(codes))
    out["etf_premium"] = _safe(errors, "etf_premium", fund.etf_premium, codes)
    out["connect_flow"] = _safe(errors, "connect_flow", capital.connect_flow)
    out["sector_flow_in"] = _safe(errors, "sector_flow", capital.sector_flow, "industry", 5)
    out["sector_flow_out"] = _safe(errors, "sector_flow", capital.sector_flow, "industry", 5, True)
    if mode == "premarket":
        out["flash_news"] = _safe(errors, "flash_news", news.flash_news, 30)
    out["market_pulse"] = _safe(errors, "market_pulse", market_pulse.pulse)
    if out["market_pulse"] and out["market_pulse"].get("errors"):
        errors.extend(f"market_pulse {e}" for e in out["market_pulse"]["errors"])
    if mode == "premarket":
        sh = _safe(errors, "etf_shares", market_pulse.etf_shares)
        if sh:
            out["market_pulse"] = out["market_pulse"] or {}
            out["market_pulse"]["shares"] = sh
            errors.extend(f"etf_shares {e}" for e in sh.get("errors") or [])
    out["policy_news"] = _safe(errors, "policy_news", news.policy_news, _news_since(mode, out.get("market")),
                               25 if mode == "premarket" else 3)
    out["errors"] = errors
    out["generated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _cache(out, mode)
    return out


def _news_since(mode, market):
    now = datetime.datetime.now()
    if mode != "premarket":
        return (now - datetime.timedelta(minutes=35)).strftime("%Y-%m-%d %H:%M:%S")
    d = (market or {}).get("a_last_quote_date") or ""
    if len(d) == 8 and d != now.strftime("%Y%m%d"):
        return f"{d[:4]}-{d[4:6]}-{d[6:]} 15:00:00"
    return (now - datetime.timedelta(hours=18)).strftime("%Y-%m-%d %H:%M:%S")


def _watch(errors, mode, held):
    items = [w for w in (_safe(errors, "watchlist", wl.watchlist) or []) if w["code"] not in held]
    if not items:
        return []
    rows, errs = wl.watch_rows(items)
    errors.extend(errs)
    if mode == "premarket":
        wcodes = [w["code"] for w in items]
        by = {r["code"]: r for r in rows}
        for r in _safe(errors, "sync_announcements(watch)", archive.sync_announcements, wcodes, 3) or []:
            if r.get("status") == "error":
                errors.append(f"announcements {r['code']}: {r['error']}（已用本地档案）")
            if r["code"] in by:
                by[r["code"]]["announcements_new"] = r.get("new", [])
        prof = _safe(errors, "sync_profiles(watch)", archive.sync_profiles, wcodes, False, 7) or []
        errors.extend(f"profile {r['code']}: {r['error']}" for r in prof if r["status"] == "error")
    return rows


def _cache(out, mode):
    """缓存最近一次 snapshot（供 build_dashboard.py 使用），失败不影响返回。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        text = json.dumps(out, ensure_ascii=False, default=str)
        for name in (f"snapshot_{mode}.json", "snapshot_latest.json"):
            tmp = os.path.join(CACHE_DIR, name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, os.path.join(CACHE_DIR, name))
    except OSError:
        pass
