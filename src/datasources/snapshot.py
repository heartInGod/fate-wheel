"""汇总快照：一次性拉取盘前 / 盘中分析需要的全部数据，单项失败不影响整体。"""
import datetime
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from . import archive, capital, commodity, fund, market_pulse, news, quotes
from . import thesis as th
from . import watchlist as wl
from .holdings import ROOT, account, holdings, merge_position

try:
    import logroll
except ImportError:  # 脱离 main.py 直接跑本脚本时，common/ 已在 sys.path（paths.py 保证）
    logroll = None

CACHE_DIR = os.path.join(ROOT, "cache")

# 本次快照的阶段耗时汇总：[(label, seconds)]，供结尾一次性排名输出。
# per_stock 在线程池里并发调用 _safe，故用锁保护。
_STAGES = []
_STAGES_LOCK = threading.Lock()


def _record(label, dt):
    with _STAGES_LOCK:
        _STAGES.append((label, dt))


@contextmanager
def _timed(label):
    """给一段串行/并发汇总之类的「块」计时，记入汇总与系统日志。"""
    t0 = time.time()
    try:
        yield
    finally:
        _record(label, time.time() - t0)


def _summary(mode, total):
    """把本次快照全部阶段按耗时降序，写成一条 SNAPSHOT SUMMARY 日志。"""
    if not logroll:
        return
    with _STAGES_LOCK:
        rows = sorted(_STAGES, key=lambda x: -x[1])
    # 同名阶段（如逐只 tech_levels）合并：次数 / 合计 / 最大，避免刷屏又能看清单项大头
    agg = {}
    for label, dt in rows:
        base = label.split(" ")[0] if label.startswith(("tech_levels ", "fund_flow_daily ", "margin ", "stock_news ")) else label
        a = agg.setdefault(base, [0, 0.0, 0.0])
        a[0] += 1
        a[1] += dt
        a[2] = max(a[2], dt)
    ranked = sorted(agg.items(), key=lambda kv: -kv[1][1])
    parts = [f"{name}:{n}次/{s:.2f}s(max {mx:.2f}s)" for name, (n, s, mx) in ranked]
    logroll.logger("snapshot").info(
        "SNAPSHOT SUMMARY mode=%s total=%.2fs stages=%d :: %s",
        mode, total, len(_STAGES), " | ".join(parts), extra={"file_only": True})


def _safe(errors, label, fn, *a, **kw):
    """调用单个数据源，失败记入 errors 不影响整体；耗时/失败记入系统日志与汇总。"""
    t0 = time.time()
    try:
        result = fn(*a, **kw)   # 不能在此直接 return：否则下面的 else 永不执行，耗时记录失效
    except Exception as e:  # noqa: BLE001
        errors.append(f"{label}: {e}")
        dt = time.time() - t0
        _record(label + "(失败)", dt)
        if logroll:
            logroll.logger("snapshot").warning(
                "STAGE snapshot.%s FAILED in %.3fs: %s: %s", label, dt,
                type(e).__name__, e, extra={"file_only": True})
        return None
    dt = time.time() - t0
    _record(label, dt)
    if logroll:
        # 超过 2 秒的阶段用 INFO 记耗时，快速阶段跳过以免刷屏（但都进了汇总）
        if dt >= 2.0:
            logroll.logger("snapshot").info(
                "STAGE snapshot.%s done in %.3fs", label, dt, extra={"file_only": True})
    return result


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
    t_all = time.time()
    with _STAGES_LOCK:
        _STAGES.clear()
    errors = []
    hs = holdings()
    codes = [h["code"] for h in hs]
    by_code = {h["code"]: h for h in hs}
    with _timed("block.market_account_indices"):
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

    with _timed("block.thesis"):
        ths = _safe(errors, "thesis", th.summaries, codes) or {}
    cids = sorted({i for t in ths.values() for i in (t or {}).get("commodities") or []})
    with _timed("block.per_stock_pool"):
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

    with _timed("block.sync_announcements"):
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
        with _timed("block.sync_profiles"):
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
    with _timed("block.watchlist"):
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
    with _timed("block.cache_write"):
        _cache(out, mode)
    _summary(mode, time.time() - t_all)
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
