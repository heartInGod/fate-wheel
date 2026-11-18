"""行情类接口：腾讯财经（日 K 失败时回退东方财富）。"""
import datetime
import json
import os
import re

from ._http import DataSourceError, http_get, http_json, normalize, to_float

DEFAULT_INDICES = {
    "sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指", "sh000688": "科创50",
    "hkHSI": "恒生指数", "hkHSTECH": "恒生科技", "usNDX": "纳斯达克100", "usIXIC": "纳斯达克",
    "usINX": "标普500", "usDJI": "道琼斯",
}


def _fetch_tencent(symbols):
    raw = http_get("https://qt.gtimg.cn/q=" + ",".join(symbols), encoding="gbk")
    out = {}
    for line in raw.split(";"):
        m = re.search(r'v_(\w+)="(.*)"', line.strip(), re.S)
        if m and m.group(2):
            out[m.group(1)] = m.group(2).split("~")
    return out


def _quote_date(ts):
    """腾讯时间戳有 20261029150003 / 2026/10/29 15:47:35 / 2026-10-28 17:15:59 三种格式。"""
    d = re.sub(r"\D", "", ts or "")
    return d[:8]


def _g(f, i):
    return f[i] if len(f) > i else ""


def realtime_quotes(codes):
    """A股/港股/ETF 实时行情。codes: 代码列表。ETF 额外返回 IOPV、溢价率。"""
    norms = [normalize(c) for c in codes]
    data = _fetch_tencent([n["tencent"] for n in norms])
    today = datetime.date.today().strftime("%Y%m%d")
    rows = []
    for n in norms:
        f = data.get(n["tencent"])
        if not f:
            rows.append({"code": n["code"], "error": "无行情"})
            continue
        is_hk = n["market"] == "hk"
        row = {
            "code": n["code"], "market": n["market"], "name": f[1],
            "price": to_float(f[3]), "prev_close": to_float(f[4]), "open": to_float(f[5]),
            "high": to_float(_g(f, 33)), "low": to_float(_g(f, 34)),
            "change": to_float(_g(f, 31)), "pct": to_float(_g(f, 32)),
            "volume": to_float(f[6]), "amount_wan": to_float(_g(f, 37)),
            "turnover_rate": to_float(_g(f, 38)),
            "pe_ttm": to_float(_g(f, 39)),
            "amplitude": to_float(_g(f, 43)),
            "quote_time": _g(f, 30), "is_today": _quote_date(_g(f, 30)) == today,
        }
        if not is_hk:
            row.update({
                "float_mv_yi": to_float(_g(f, 44)), "total_mv_yi": to_float(_g(f, 45)),
                "pb": to_float(_g(f, 46)), "limit_up": to_float(_g(f, 47)),
                "limit_down": to_float(_g(f, 48)), "volume_ratio": to_float(_g(f, 49)),
            })
            if _g(f, 61).strip() == "ETF" or _g(f, 61).strip().startswith("LOF"):
                row.update({
                    "is_etf": True,
                    "premium_pct": to_float(_g(f, 77)),
                    "iopv": to_float(_g(f, 78)),
                    "nav_prev": to_float(_g(f, 81)),
                })
        rows.append(row)
    return rows


def index_quotes(symbols=None):
    """主要指数（A股/港股/美股）。symbols 为腾讯代码，默认上证、深成、创业板、科创50、恒指、恒生科技、纳指100、纳指、标普、道指。"""
    syms = list(symbols) if symbols else list(DEFAULT_INDICES)
    data = _fetch_tencent(syms)
    rows = []
    for s in syms:
        f = data.get(s)
        if not f:
            rows.append({"symbol": s, "error": "无行情"})
            continue
        rows.append({
            "symbol": s, "name": DEFAULT_INDICES.get(s, f[1]),
            "price": to_float(f[3]), "prev_close": to_float(f[4]),
            "change": to_float(_g(f, 31)), "pct": to_float(_g(f, 32)),
            "high": to_float(_g(f, 33)), "low": to_float(_g(f, 34)),
            "quote_time": _g(f, 30),
        })
    return rows


def market_status():
    """判断今天 A股/港股 是否开市（以指数最新行情日期是否为今天判断）。"""
    today = datetime.date.today().strftime("%Y%m%d")
    data = _fetch_tencent(["sh000001", "hkHSI"])
    a = _quote_date(_g(data.get("sh000001", []), 30))
    h = _quote_date(_g(data.get("hkHSI", []), 30))
    now = datetime.datetime.now()
    hm = now.hour * 100 + now.minute
    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "a_open_today": a == today, "a_last_quote_date": a,
        "hk_open_today": h == today, "hk_last_quote_date": h,
        "a_in_session": a == today and (930 <= hm <= 1130 or 1300 <= hm <= 1500),
    }


_KLINE_HOSTS = ("https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn", "https://proxy.finance.qq.com/ifzqgtimg")


from fw.paths import ROOT
KLINE_CACHE_DIR = os.path.join(ROOT, "cache", "kline")


def _kline_cache_path(code):
    return os.path.join(KLINE_CACHE_DIR, normalize(code)["tencent"] + ".json")


def cached_kline(code):
    """本地缓存的前复权日 K（kline() 每次成功拉取都会写入）。返回 (rows, 抓取时间)；没有缓存返回 ([], None)。不联网。"""
    try:
        with open(_kline_cache_path(code), encoding="utf-8") as f:
            j = json.load(f)
        return j.get("rows") or [], j.get("fetched_at")
    except (OSError, ValueError):
        return [], None


def _save_kline(code, rows):
    """按日期合并进缓存（新数据覆盖同日，今天这根盘中会变），最多保留 250 根。写失败不影响主流程。"""
    try:
        old, _ = cached_kline(code)
        by = {r["date"]: r for r in old}
        by.update({r["date"]: r for r in rows})
        merged = [by[d] for d in sorted(by)][-250:]
        os.makedirs(KLINE_CACHE_DIR, exist_ok=True)
        path = _kline_cache_path(code)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "rows": merged}, f)
        os.replace(tmp, path)
    except (OSError, KeyError, TypeError):
        pass


# normalize 认不出的指数代码：腾讯代码 → 东财 secid
_INDEX_SECID = {"hkHSI": "100.HSI", "hkHSTECH": "124.HSTECH"}
_EM_KLT = {"day": 101, "week": 102, "month": 103}
_EM_FQT = {"qfq": 1, "hfq": 2, "": 0}


def _kline_ids(code):
    if code in _INDEX_SECID:
        return code, _INDEX_SECID[code]
    n = normalize(code)
    return n["tencent"], n["secid"]


def _tencent_kline(sym, period, count, adjust):
    # 2026-11-06 起旧路径 fqkline 被腾讯 WAF 封（HTTP 501），新路径 newfqkline 正常且数据一致；三个域名依次试
    path = f"/appstock/app/newfqkline/get?param={sym},{period},,,{int(count)},{adjust}"
    last = None
    for host in _KLINE_HOSTS:
        try:
            j = http_json(host + path, retries=0)
        except DataSourceError as e:
            last = e
            continue
        node = (j.get("data") or {}).get(sym) or {}
        rows = node.get(adjust + period) or node.get(period) or []
        if rows:
            return [{"date": r[0], "open": to_float(r[1]), "close": to_float(r[2]), "high": to_float(r[3]),
                     "low": to_float(r[4]), "volume": to_float(r[5])} for r in rows]
        last = DataSourceError(f"{sym} 腾讯无 K 线数据")
    raise last


def _em_kline(secid, period, count, adjust):
    # 东财 push2his：必须带 end，否则返回空；成交量单位与腾讯一致（A股/ETF 为手，港股为股）
    j = http_json("https://push2his.eastmoney.com/api/qt/stock/kline/get"
                  f"?secid={secid}&klt={_EM_KLT[period]}&fqt={_EM_FQT[adjust]}&lmt={int(count)}&end=20500101"
                  "&fields1=f1&fields2=f51,f52,f53,f54,f55,f56", retries=1)
    out = []
    for s in ((j.get("data") or {}).get("klines") or []):
        p = s.split(",")
        out.append({"date": p[0], "open": to_float(p[1]), "close": to_float(p[2]), "high": to_float(p[3]),
                    "low": to_float(p[4]), "volume": to_float(p[5])})
    if not out:
        raise DataSourceError(f"{secid} 东财无 K 线数据")
    return out


def kline(code, period="day", count=60, adjust="qfq"):
    """历史 K 线。period: day/week/month；adjust: qfq 前复权 / hfq / 空串不复权。
    主源腾讯 newfqkline，失败（含熔断中）回退东财 push2his；两源前复权算法相同，120 日实测逐根一致。"""
    sym, secid = _kline_ids(code)
    try:
        out = _tencent_kline(sym, period, count, adjust)
    except DataSourceError as e1:
        try:
            out = _em_kline(secid, period, count, adjust)
        except DataSourceError as e2:
            raise DataSourceError(f"{code} K 线两源均失败：腾讯 {e1}；东财 {e2}") from e2
    out = out[-int(count):]
    if period == "day" and adjust == "qfq" and code not in _INDEX_SECID:
        _save_kline(code, out)
    return out


def tech_levels(code, count=120):
    """由日 K 计算技术位：均线、近 20/60 日高低点、相对均线位置、近 5 日涨跌。"""
    k = kline(code, "day", count)
    closes = [r["close"] for r in k]

    def ma(n):
        return round(sum(closes[-n:]) / n, 4) if len(closes) >= n else None

    def hi(n):
        return max(r["high"] for r in k[-n:])

    def lo(n):
        return min(r["low"] for r in k[-n:])

    last = closes[-1]
    out = {"code": normalize(code)["code"], "date": k[-1]["date"], "close": last,
           "ma5": ma(5), "ma10": ma(10), "ma20": ma(20), "ma60": ma(60),
           "high20": hi(20), "low20": lo(20), "high60": hi(min(60, len(k))), "low60": lo(min(60, len(k))),
           "prev_high": k[-2]["high"] if len(k) > 1 else None,
           "prev_low": k[-2]["low"] if len(k) > 1 else None,
           "chg5_pct": round((last / closes[-6] - 1) * 100, 2) if len(closes) > 5 else None}
    if out["ma20"]:
        out["vs_ma20_pct"] = round((last / out["ma20"] - 1) * 100, 2)
    return out
