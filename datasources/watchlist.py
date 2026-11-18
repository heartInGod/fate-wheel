"""自选股（观察名单）：watchlist.csv + 按天缓存的技术位 + 快照用精简行情。

watchlist.csv 列：代码,名称,行业,加入日期,来源,关注要点（由人维护，定时任务只读）。
自选股不是持仓，只用来找买点（👀自选低吸 / 👀自选突破），见 intraday_rules.md「自选股」。

技术位只用「今天之前」的日 K 计算（盘中 K 线最后一根是今天，会被剔除），
所以 high20/low20/ma* 都是昨天收盘时的值，盘中拿现价/日内高低去比较不会自我包含。
每天第一次调用时拉取并写 cache/watch_tech.json，之后同一天直接读缓存（盘中每 10 分钟不再拉 30 次 K 线）。
"""
import csv
import datetime
import json
import os
from concurrent.futures import ThreadPoolExecutor

from . import quotes
from ._http import normalize
from .holdings import ROOT
from datamul import data_path

WATCHLIST_CSV = data_path("watchlist.csv")
TECH_CACHE = os.path.join(ROOT, "cache", "watch_tech.json")

ROW_KEYS = ("code", "name", "price", "pct", "change", "high", "low", "open", "prev_close", "volume_ratio",
            "amplitude", "turnover_rate", "amount_wan", "limit_up", "limit_down", "total_mv_yi",
            "pe_ttm", "quote_time", "is_today")


def watchlist():
    """读取 watchlist.csv → [{code,name,industry,added,source,focus}]；文件不存在返回空列表。"""
    if not os.path.exists(WATCHLIST_CSV):
        return []
    with open(WATCHLIST_CSV, encoding="utf-8-sig", newline="") as f:
        out = []
        for r in csv.DictReader(f):
            c = (r.get("代码") or "").strip()
            if not c:
                continue
            out.append({"code": normalize(c)["code"], "name": (r.get("名称") or "").strip(),
                        "industry": (r.get("行业") or "").strip(), "added": (r.get("加入日期") or "").strip(),
                        "source": (r.get("来源") or "").strip(), "focus": (r.get("关注要点") or "").strip()})
        return out


def _tech_one(code, today):
    return tech_from_klines(quotes.kline(code, "day", 130), today)


def tech_from_klines(k, today):
    """由日 K（[{date,open,close,high,low,volume}]）算自选股技术位，只用昨天及以前。"""
    k = list(k or [])
    if k and str(k[-1]["date"]).replace("-", "") == today:
        k = k[:-1]  # 剔除今天这根，只用昨天及以前
    if len(k) < 2:
        raise ValueError("K 线不足")
    closes = [r["close"] for r in k]

    def ma(n):
        return round(sum(closes[-n:]) / n, 4) if len(closes) >= n else None

    n60 = min(60, len(k))
    vols = [r["volume"] or 0 for r in k[-5:]]
    return {"date": k[-1]["date"], "close": closes[-1],
            "ma5": ma(5), "ma10": ma(10), "ma20": ma(20), "ma60": ma(60),
            "high20": max(r["high"] for r in k[-20:]), "low20": min(r["low"] for r in k[-20:]),
            "high60": max(r["high"] for r in k[-n60:]), "low60": min(r["low"] for r in k[-n60:]),
            "prev_high": k[-1]["high"], "prev_low": k[-1]["low"],
            "chg5_pct": round((closes[-1] / closes[-6] - 1) * 100, 2) if len(closes) > 5 else None,
            "chg20_pct": round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) > 20 else None,
            "avg_vol5": round(sum(vols) / len(vols), 0) if vols else None}


def watch_tech(codes, force=False):
    """返回 ({code: tech}, errors)。同一天读缓存，缺的才补拉。"""
    today = datetime.date.today().strftime("%Y%m%d")
    cache = {}
    if not force and os.path.exists(TECH_CACHE):
        try:
            with open(TECH_CACHE, encoding="utf-8") as f:
                j = json.load(f)
            if j.get("day") == today:
                cache = j.get("tech") or {}
        except (OSError, json.JSONDecodeError):
            cache = {}
    miss = [c for c in codes if c not in cache]
    errors = []
    if miss:
        def one(c):
            try:
                return c, _tech_one(c, today), None
            except Exception as e:  # noqa: BLE001
                return c, None, f"watch_tech {c}: {e}"
        with ThreadPoolExecutor(max_workers=6) as ex:
            for c, t, e in ex.map(one, miss):
                if t:
                    cache[c] = t
                if e:
                    errors.append(e)
        try:
            os.makedirs(os.path.dirname(TECH_CACHE), exist_ok=True)
            tmp = TECH_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"day": today, "tech": cache}, f, ensure_ascii=False)
            os.replace(tmp, TECH_CACHE)
        except OSError:
            pass
    return {c: cache[c] for c in codes if c in cache}, errors


def watch_rows(items=None):
    """快照用：自选股实时行情（一次批量请求）+ 技术位。返回 (rows, errors)。"""
    items = items if items is not None else watchlist()
    if not items:
        return [], []
    codes = [w["code"] for w in items]
    errors = []
    try:
        qs = {q["code"]: q for q in quotes.realtime_quotes(codes)}
    except Exception as e:  # noqa: BLE001
        qs = {}
        errors.append(f"watch realtime_quotes: {e}")
    tech, errs = watch_tech(codes)
    errors.extend(errs)
    rows = []
    for w in items:
        q = qs.get(w["code"]) or {}
        r = {k: q.get(k) for k in ROW_KEYS}
        r.update({"code": w["code"], "name": w["name"] or q.get("name"), "industry": w["industry"],
                  "focus": w["focus"], "watch": True, "tech": tech.get(w["code"])})
        if q.get("error"):
            r["error"] = q["error"]
        rows.append(r)
    return rows, errors
