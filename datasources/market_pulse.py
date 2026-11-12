"""大盘资金脉搏：宽基 ETF 同时刻量能，用来识别“国家队”（中央汇金等）入场护盘的迹象。

只采集原始指标，不做判断；等级判定在 signals_engine.classify_pulse（阈值在 rules_params.json 的 market_pulse 段）。

指标口径：
  vol_ratio   今天开盘到当前 5 分钟 K 的累计成交量 ÷ 前 5 个交易日同一时刻累计成交量的均值（腾讯 5 分钟 K）。
              盘前运行时“今天”就是上一交易日，等于全天量比。
  ret_15m     最近 3 根 5 分钟 K 的涨跌（%），不足 3 根时相对昨收
  from_low    现价相对日内最低价的反弹幅度（%）
历史校准（日线全天量比）：2025-04-07 汇金公告增持 ETF 当天，4 只沪深300ETF 为 7~18 倍、中证1000ETF 2.6 倍；
2025-04-08 为 5~14 倍；2024-02-07 约 3 倍。2026-11-02 小盘反弹日沪深300ETF 中位约 1.2 倍、中证1000ETF 2.7 倍。
"""
import datetime
import statistics
from concurrent.futures import ThreadPoolExecutor

from ._http import http_json, normalize, to_float
from . import quotes

# (代码, 名称, 分组)。hs300 是汇金历来增持的主力品种；a500 为 2024 年后新增的增持品种
BROAD_ETFS = [
    ("510300", "沪深300ETF华泰柏瑞", "hs300"), ("510310", "沪深300ETF易方达", "hs300"),
    ("510330", "沪深300ETF华夏", "hs300"), ("159919", "沪深300ETF嘉实", "hs300"),
    ("510050", "上证50ETF华夏", "sz50"),
    ("512050", "中证A500ETF华夏", "a500"), ("563360", "中证A500ETF华泰柏瑞", "a500"),
    ("510500", "中证500ETF南方", "small"), ("512100", "中证1000ETF南方", "small"),
    ("588000", "科创50ETF华夏", "growth"), ("159915", "创业板ETF易方达", "growth"),
]
PULSE_INDICES = {"sh000300": "沪深300", "sh000016": "上证50", "sh000905": "中证500", "sh000852": "中证1000"}

_M5_HOSTS = ("https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn")


def m5_bars(code, count=320):
    """5 分钟 K：{yyyymmdd: [(HHMM, close, high, low, volume)]}，按时间排序。"""
    s = normalize(code)["tencent"]
    j, last = None, None
    for host in _M5_HOSTS:
        try:
            j = http_json(f"{host}/appstock/app/kline/mkline?param={s},m5,,{int(count)}", retries=0)
            break
        except Exception as e:  # noqa: BLE001
            last = e
    if j is None:
        raise last
    rows = ((j.get("data") or {}).get(s) or {}).get("m5") or []
    out = {}
    for r in rows:
        out.setdefault(r[0][:8], []).append(
            (r[0][8:12], to_float(r[2]), to_float(r[3]), to_float(r[4]), to_float(r[5]) or 0.0))
    return out


def etf_metrics(code, base_days=5):
    days = m5_bars(code)
    if not days:
        return {"error": "无 5 分钟 K"}
    ds = sorted(days)
    d0 = ds[-1]
    bars = days[d0]
    t = bars[-1][0]
    # 盘中最后一根 5 分钟 K 尚未走完（时间戳为该 K 的结束时刻），量比只算已完成的 K
    now = datetime.datetime.now()
    if d0 == now.strftime("%Y%m%d") and now.strftime("%H%M") < t:
        if len(bars) > 1:
            t = bars[-2][0]
        elif len(ds) > 1:
            # 9:25~9:35 之间今天只有一根带集合竞价量的未完成 K：今天按未开盘处理，回退为上一交易日全天
            ds = ds[:-1]
            d0 = ds[-1]
            bars = days[d0]
            t = bars[-1][0]
    cum = sum(b[4] for b in bars if b[0] <= t)
    prev = []
    for d in ds[-1 - base_days:-1]:
        prev.append(sum(b[4] for b in days[d] if b[0] <= t))
    base = statistics.mean(prev) if prev else None
    price = bars[-1][1]
    if len(bars) >= 4:
        ref = bars[-4][1]
    elif len(ds) >= 2:
        ref = days[ds[-2]][-1][1]
    else:
        ref = None
    low = min(b[3] for b in bars if b[3])
    prev_close = days[ds[-2]][-1][1] if len(ds) >= 2 else None
    pct = lambda a, b: round((a / b - 1) * 100, 2) if a and b else None
    return {
        "day": d0, "bar_time": t, "bars": len(bars), "base_days": len(prev),
        "vol_ratio": round(cum / base, 2) if base else None,
        "price": price, "pct": pct(price, prev_close),
        "ret_15m": pct(price, ref), "from_low": pct(price, low),
    }


def pulse(base_days=5):
    """宽基 ETF 量能 + 相关指数。返回 {as_of, etfs:[...], groups:{group: {median_ratio, ratios}}, indices:[...]}。"""
    def one(item):
        code, name, group = item
        try:
            m = etf_metrics(code, base_days)
        except Exception as e:  # noqa: BLE001
            m = {"error": str(e)}
        return {"code": code, "name": name, "group": group, **m}

    with ThreadPoolExecutor(max_workers=6) as ex:
        etfs = list(ex.map(one, BROAD_ETFS))
    groups = {}
    for e in etfs:
        if e.get("vol_ratio") is not None:
            groups.setdefault(e["group"], []).append(e["vol_ratio"])
    gsum = {g: {"median_ratio": round(statistics.median(v), 2), "ratios": v} for g, v in groups.items()}
    idx = []
    try:
        for r in quotes.index_quotes(list(PULSE_INDICES)):
            r["name"] = PULSE_INDICES.get(r["symbol"], r.get("name"))
            if r.get("price") and r.get("low"):
                r["from_low"] = round((r["price"] / r["low"] - 1) * 100, 2)
            idx.append(r)
    except Exception as e:  # noqa: BLE001
        idx.append({"error": str(e)})
    days = [e.get("day") for e in etfs if e.get("day")]
    times = [e.get("bar_time") for e in etfs if e.get("bar_time")]
    return {
        "as_of": f"{max(days)} {max(times)}" if days and times else None,
        "collected_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "etfs": etfs, "groups": gsum, "indices": idx,
        "errors": [f"{e['code']}: {e['error']}" for e in etfs if e.get("error")],
    }


# ---------------- ③ 宽基 ETF 份额（申购赎回净额，收盘后才公布，只用于盘前） ----------------
_SSE_SHARES = ("https://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
               "&isPagination=true&pageHelp.pageSize=2000&pageHelp.pageNo=1&STAT_DATE={d}")


def sse_shares(date):
    """上交所全部 ETF 某日总份额：{代码: 万份}。当日数据收盘后才公布，未公布时返回 {}。"""
    j = http_json(_SSE_SHARES.format(d=date), headers={"Referer": "https://www.sse.com.cn/"}, retries=1)
    return {x["SEC_CODE"]: to_float(x.get("TOT_VOL")) for x in j.get("result") or [] if x.get("SEC_CODE")}


def etf_shares(days=5):
    """沪市宽基 ETF 最近 days 个交易日的份额日变化（深市 159919/159915 无历史接口，不含）。
    返回 {date, prev_date, etfs:[{code,name,group,shares_yi,chg_pct,chg_yi}],
          groups:{group: {chg_pct, chg_yi}}, history:[{date, groups:{group: chg_pct}}]（新→旧）, errors}。
    份额净增 = 当天净申购。上交所接口并发会被限流，按交易日从新到旧串行拉取（每次约 0.3 秒）。
    校准（沪市 3 只沪深300ETF 合计）：汇金增持日 2025-04-07 +5.2%、04-08 +6.0%、2024-02-07 +7.1%；平常 ±1%。"""
    today = datetime.date.today().isoformat()
    try:
        tds = [k["date"] for k in quotes.kline("510300", "day", days + 6, "") if k.get("date")]
    except Exception as e:  # noqa: BLE001
        return {"errors": [f"交易日历: {e}"]}
    tds = [d for d in tds if d <= today]
    sh = [x for x in BROAD_ETFS if not x[0].startswith("15")]
    got, errors = {}, []
    for d in reversed(tds):
        r = _try(sse_shares, d)
        if isinstance(r, str):
            errors.append(f"{d}: {r[-80:]}")
        elif r:
            got[d] = r
        elif not got:
            continue                      # 最新交易日（如今天盘中）尚未公布，不算错误
        if len([x for x in tds if x in got]) >= days + 1 or len(errors) >= 3:
            break
    hist = []
    for d0, d1 in zip(tds, tds[1:]):      # 只用相邻交易日做差，缺一天就跳过
        if d0 not in got or d1 not in got:
            continue
        a, b = got[d0], got[d1]
        g, per = {}, []
        for code, name, grp in sh:
            if a.get(code) and b.get(code):
                s = g.setdefault(grp, [0.0, 0.0])
                s[0] += a[code]
                s[1] += b[code]
                per.append({"code": code, "name": name, "group": grp, "shares_yi": round(b[code] / 1e4, 1),
                            "chg_pct": round((b[code] / a[code] - 1) * 100, 2), "chg_yi": round((b[code] - a[code]) / 1e4, 1)})
        hist.append({"date": d1, "prev_date": d0, "etfs": per,
                     "groups": {k: {"chg_pct": round((v[1] / v[0] - 1) * 100, 2), "chg_yi": round((v[1] - v[0]) / 1e4, 1)}
                                for k, v in g.items() if v[0]}})
    if not hist:
        return {"errors": errors or ["无可用份额数据"]}
    last = hist[-1]
    return {"date": last["date"], "prev_date": last["prev_date"], "etfs": last["etfs"], "groups": last["groups"],
            "history": [{"date": h["date"], "groups": {k: v["chg_pct"] for k, v in h["groups"].items()}}
                        for h in reversed(hist)][:days],
            "errors": errors}


def _try(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return str(e)
