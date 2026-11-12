#!/usr/bin/env python3
"""ETF 长线评估：指数估值法。决定一只 ETF 能否进长线仓，进了之后按什么价格买卖。

个股用「一致预期 EPS × 合理 PE」定目标价；ETF 是一篮子股票，没有单一公司的财报和一致预期，改用它跟踪的指数：
长期持有指数的收益 ≈ 股息 + 盈利增长 + 估值变化（博格公式），所以看三件事——
  1. 盈利长期在增长吗（准入）：指数盈利 = 点位 / PE(TTM)，近 10 年（不足 10 年按已有年数，至少 hist_min_years 年）
     年化 ≥ earn_cagr_min%。盈利长期萎缩的指数只能做波段，不长期拿。
  2. 估值能比较吗（准入）：估值历史 ≥ hist_min_years 年；近 10 年 PE ≤ 0 或 > pe_abs_max 的天数占比 ≤ bad_share_max%，
     且当前 PE 正常——盈利常年接近零的指数，PE 分位没有意义。
  3. 现在贵不贵（定价）：
     周期修正盈利 E_norm = min(当前盈利, 近 3 年平均盈利 × (1+长期增速)^1.5 × (1+cyc_cap_pct%))；
       长期增速 = 上面的盈利年化，限制在 [0, earn_growth_cap%]。盈利暴增（周期顶部）时 PE 会被摊低、看着便宜，
       超出趋势 cyc_cap_pct% 以上的那部分盈利不算，避免在周期顶部买入。
     合理 PE = 近 pe_hist_years 年 PE(TTM) 中位数（剔除异常值）；修正 PE = 点位 / E_norm；
     目标点位 = E_norm × 合理 PE（即估值回到中位数，不外推未来增长；增长会在每周复查时自然抬高目标价）；
     ETF 目标价 = 最新单位净值 × 目标点位 / 净值同日的指数点位（按净值算，场内溢价不算进价值）；
     分区用「现价 / (1+场内溢价)」与目标价比较——判断的是指数贵不贵，溢价只管买不买（见下）。
另外还有三条 ETF 本身的条件：规模 ≥ min_scale_yi 亿、日成交额 ≥ min_amount_wan 万（防清盘、防流动性差）；
管理费 ≤ fee_max_pct%。同一个估值指数只放一只进长线仓（已在长线仓的优先，其次持仓市值大的）。

归属（core_value.review 调用 judge/assign）：
  满足全部准入条件且在买入区/持有区 → 长线仓；满足条件但已在卖出区/清仓区（估值偏高）→ 暂留交易仓，回到持有区后再转；
  不满足 → 交易仓。已在长线仓的不会因某次复查不满足而自动移出，标「逻辑复查」由人决定。
分区：空间 = 目标价/现价 − 1；空间 ≥ buy_upside_pct% 买入区（指数分散了个股风险，门槛比个股低）；
  到目标价卖一半，到目标价 × sell_all_ratio 清仓（与个股相同）。场内溢价 > prem_max_pct% 时不买（QDII 常见）。
逻辑复查（长线仓 ETF）：指数盈利同比 < −earn_drop_review_pct%；不再满足准入条件。
估值数据：中证指数官网 index-perf（日线带 PE TTM）；海外指数用蛋卷基金 PE 历史 + 新浪点位。
ETF 与估值指数的对应关系写在 rules_params.json sleeves.core.etf.index_map；没配的 ETF 不进长线仓。
"""
import bisect
import datetime
import json
import re
import statistics

from datasources._http import DataSourceError, http_get, http_json

DEFAULTS = {"hist_min_years": 5, "pe_hist_years": 10, "pe_abs_max": 150, "bad_share_max": 20, "earn_cagr_min": 0,
            "earn_growth_cap": 25, "cyc_cap_pct": 20, "min_scale_yi": 20, "min_amount_wan": 3000,
            "fee_max_pct": 0.6, "buy_upside_pct": 20, "prem_max_pct": 3, "earn_drop_review_pct": 30}


def _f(v):
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _d(s):
    return datetime.datetime.strptime(s, "%Y%m%d")


# ----------------------------------------------------------------- 取数
def fetch_series(entry):
    """估值指数日线 [(yyyyMMdd, 点位, PE_TTM)]，按日期升序。"""
    src, code = entry.get("source"), entry.get("code")
    today = datetime.date.today().strftime("%Y%m%d")
    if src == "csindex":
        j = http_json("https://www.csindex.com.cn/csindex-home/perf/index-perf",
                      params={"indexCode": code, "startDate": "20100101", "endDate": today},
                      headers={"Referer": "https://www.csindex.com.cn/"}, timeout=30, retries=2)
        rows = [(x["tradeDate"], _f(x.get("close")), _f(x.get("peg"))) for x in (j.get("data") or [])]
        return sorted(r for r in rows if r[1] and r[2] is not None)
    if src == "danjuan":
        pe = (http_json(f"https://danjuanfunds.com/djapi/index_eva/pe_history/{code}", params={"day": "all"},
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=20, retries=2).get("data") or {}).get(
            "index_eva_pe_growths") or []
        sym = entry.get("close_symbol")
        if not sym:
            raise DataSourceError(f"{code} 缺 close_symbol（新浪美股指数代码，如 .ndx）")
        t = http_get("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/x/US_MinKService.getDailyK",
                     params={"symbol": sym, "type": "daily"}, timeout=20, retries=2)
        k = json.loads(t[t.index("x(") + 2:t.rindex(")")])
        cl = {r["d"].replace("-", ""): _f(r["c"]) for r in k if _f(r.get("c"))}
        ks = sorted(cl)
        out = []
        for x in pe:
            d = datetime.datetime.fromtimestamp(x["ts"] / 1000).strftime("%Y%m%d")
            i = bisect.bisect_right(ks, d) - 1
            if i >= 0 and _f(x.get("pe")) is not None:
                out.append((d, cl[ks[i]], _f(x["pe"])))
        return sorted(out)
    raise DataSourceError(f"未知估值数据源 {src}")


def fetch_meta(codes):
    """ETF 本身：现价、成交额、规模（东方财富）；跟踪标的、管理费（F10 基本概况）；最新净值；实时溢价（腾讯 IOPV）。"""
    out = {c: {"code": c} for c in codes}
    if not codes:
        return out
    secids = [("1." if c[0] in "569" else "0.") + c for c in codes]
    j = http_json("https://push2.eastmoney.com/api/qt/ulist.np/get",
                  params={"fltt": "2", "secids": ",".join(secids), "fields": "f12,f14,f2,f6,f20"}, timeout=20, retries=2)
    for d in ((j.get("data") or {}).get("diff")) or []:
        out.setdefault(d["f12"], {"code": d["f12"]}).update(
            name=d.get("f14"), price=_f(d.get("f2")), amount_wan=round((_f(d.get("f6")) or 0) / 1e4),
            scale_yi=round((_f(d.get("f20")) or 0) / 1e8, 1))
    for c in codes:
        m = out[c]
        try:
            t = http_get(f"https://fundf10.eastmoney.com/jbgk_{c}.html", timeout=15, retries=1)
            g = re.search(r"跟踪标的</th><td[^>]*>(.*?)</td>", t)
            fee = re.search(r"管理费率</th><td[^>]*>(.*?)</td>", t)
            m["tracking"] = re.sub("<[^>]+>", "", g.group(1)).strip() if g else None
            fm = re.search(r"([\d.]+)%", re.sub("<[^>]+>", "", fee.group(1))) if fee else None
            m["fee_pct"] = _f(fm.group(1)) if fm else None
        except DataSourceError:
            pass
        try:
            from datasources.fund import etf_nav_history
            nv = etf_nav_history(c, 1)[0]
            m["nav"], m["nav_date"] = nv["nav"], nv["date"].replace("-", "")
        except (DataSourceError, IndexError, KeyError):
            pass
    try:
        from datasources.fund import etf_premium
        for p in etf_premium(codes):
            if p.get("code") in out and p.get("premium_pct") is not None:
                out[p["code"]]["premium_pct"] = p["premium_pct"]
    except DataSourceError:
        pass
    for c, m in out.items():   # 腾讯取不到时用 现价/最新净值（QDII 净值滞后，仅作近似）
        if m.get("premium_pct") is None and m.get("price") and m.get("nav"):
            m["premium_pct"] = round((m["price"] / m["nav"] - 1) * 100, 2)
    return out


# ----------------------------------------------------------------- 计算（纯函数，便于测试）
def metrics(series, cfg=None):
    """series [(yyyyMMdd, 点位, PE)] → 估值与盈利指标。"""
    cfg = dict(DEFAULTS, **(cfg or {}))
    if not series:
        return {"ok_data": False, "why": "无估值数据"}
    last = _d(series[-1][0])
    yrs = (last - _d(series[0][0])).days / 365.25
    h = [x for x in series if _d(x[0]) >= last - datetime.timedelta(days=int(365.25 * cfg["pe_hist_years"]))]
    pes = [x[2] for x in h]
    good = [p for p in pes if 0 < p <= cfg["pe_abs_max"]]
    pe, close = series[-1][2], series[-1][1]
    m = {"ok_data": True, "asof": series[-1][0], "close": close, "pe": round(pe, 2), "hist_years": round(yrs, 1),
         "bad_share": round(100 - 100 * len(good) / len(pes)) if pes else 100}
    if not good or not (0 < pe <= cfg["pe_abs_max"]):
        m.update(pe_fair=round(statistics.median(good), 1) if good else None, why="当前 PE 异常或无正常 PE 历史")
        return m
    med = statistics.median(good)
    E = [(x[0], x[1] / x[2]) for x in series if x[1] and x[2] and 0 < x[2] <= cfg["pe_abs_max"]]

    def e_at(days):
        cut = last - datetime.timedelta(days=days)
        return next((e for e in E if _d(e[0]) >= cut), None)

    e1 = close / pe
    n = min(10, int(yrs + 0.05))
    e0 = e_at(int(365.25 * n)) if n >= 1 else None
    cagr = ((e1 / e0[1]) ** (1 / n) - 1) * 100 if e0 and e0[1] > 0 else None
    y1 = e_at(365)
    e_yoy = (e1 / y1[1] - 1) * 100 if y1 and y1[1] > 0 and _d(y1[0]) < last else None
    e3 = [e[1] for e in E if _d(e[0]) >= last - datetime.timedelta(days=1096)]
    g = min(max(cagr or 0, 0), cfg["earn_growth_cap"]) / 100
    trend = statistics.mean(e3) * (1 + g) ** 1.5 if e3 else e1
    e_norm = min(e1, trend * (1 + cfg["cyc_cap_pct"] / 100))
    pe_norm = close / e_norm
    m.update(pe_fair=round(med, 1), pe_norm=round(pe_norm, 1), cyc_capped=e_norm < e1 - 1e-12,
             e_vs_trend_pct=round((e1 / trend - 1) * 100), pe_pct=round(100 * sum(p < pe for p in pes) / len(pes)),
             earn_cagr=round(cagr, 1) if cagr is not None else None, earn_years=n,
             earn_yoy=round(e_yoy, 1) if e_yoy is not None else None,
             target_idx=round(e_norm * med, 2), fair_ratio=med / pe_norm,
             price_pct=round(100 * sum(x[1] < close for x in h) / len(h)))
    return m


def close_on(series, day):
    """day（yyyyMMdd）当天或之前最近一个交易日的点位。"""
    ds = [x[0] for x in series]
    i = bisect.bisect_right(ds, day) - 1
    return series[i][1] if i >= 0 else None


def judge(m, meta, cfg=None):
    """准入：返回 (是否满足, 不满足原因 list)。"""
    cfg = dict(DEFAULTS, **(cfg or {}))
    why = []
    if not m.get("ok_data"):
        return False, [m.get("why") or "无估值数据"]
    if m["hist_years"] < cfg["hist_min_years"]:
        why.append(f"估值历史 {m['hist_years']} 年 < {cfg['hist_min_years']} 年")
    if m["bad_share"] > cfg["bad_share_max"]:
        why.append(f"PE 异常（≤0 或 >{cfg['pe_abs_max']}）天数占 {m['bad_share']}%")
    if m.get("why"):
        why.append(m["why"])
    elif m.get("earn_cagr") is None or m["earn_cagr"] < cfg["earn_cagr_min"]:
        why.append(f"指数盈利 {m.get('earn_years')} 年年化 {m.get('earn_cagr')}% < {cfg['earn_cagr_min']}%")
    if (meta.get("scale_yi") or 0) < cfg["min_scale_yi"]:
        why.append(f"规模 {meta.get('scale_yi') or 0} 亿 < {cfg['min_scale_yi']} 亿")
    if (meta.get("amount_wan") or 0) < cfg["min_amount_wan"]:
        why.append(f"成交额 {meta.get('amount_wan') or 0} 万 < {cfg['min_amount_wan']} 万")
    if meta.get("fee_pct") is not None and meta["fee_pct"] > cfg["fee_max_pct"]:
        why.append(f"管理费 {meta['fee_pct']}% > {cfg['fee_max_pct']}%")
    return not why, why


def value(m, meta, series, cfg, sell_all_ratio):
    """ETF 目标价与分区（字段与个股 valuation 对齐：target/buy_below/sell_half_at/sell_all_at/upside_pct/zone）。"""
    cfg = dict(DEFAULTS, **(cfg or {}))
    price = meta.get("price")
    if not m.get("target_idx") or not price:
        return {"zone": "na", "why": m.get("why") or "缺现价或估值"}
    base_idx = close_on(series, meta.get("nav_date") or "") if meta.get("nav") else None
    if meta.get("nav") and base_idx:
        target = meta["nav"] * m["target_idx"] / base_idx
    else:   # 没有净值时按现价折算（溢价算进去了，粗略）
        target = price * m["target_idx"] / m["close"]
    prem = meta.get("premium_pct")
    fair_px = fair_price(price, prem)
    up = (target / fair_px - 1) * 100
    z = zone(fair_px, target, cfg["buy_upside_pct"], sell_all_ratio)
    return {"target": round(target, 4), "buy_below": round(target / (1 + cfg["buy_upside_pct"] / 100), 4),
            "sell_half_at": round(target, 4), "sell_all_at": round(target * sell_all_ratio, 4),
            "upside_pct": round(up, 1), "upside_mkt_pct": round((target / price - 1) * 100, 1), "zone": z,
            "entry_block": (f"场内溢价 {prem:.1f}% > {cfg['prem_max_pct']}%，暂不买（等溢价回落或场外申购）"
                            if prem is not None and prem > cfg["prem_max_pct"] else None)}


def fair_price(price, prem):
    """按净值折算的价格：现价 / (1+溢价)。分区看指数贵不贵，场内溢价只作买入闸门，不让溢价把持有区推成卖出区。"""
    return price / (1 + prem / 100) if price and prem is not None and prem > -50 else price


def zone(px, target, buy_upside_pct, sell_all_ratio):
    if not px or not target:
        return "na"
    if px >= target * sell_all_ratio:
        return "sell_all"
    if px >= target:
        return "sell_half"
    return "buy" if (target / px - 1) * 100 >= buy_upside_pct else "hold"


def review_flags(m, ok, why, cfg=None):
    cfg = dict(DEFAULTS, **(cfg or {}))
    rv = []
    if m.get("earn_yoy") is not None and m["earn_yoy"] < -cfg["earn_drop_review_pct"]:
        rv.append(f"指数盈利同比 {m['earn_yoy']}%")
    if not ok:
        rv.append("不再满足 ETF 长线条件：" + "；".join(why))
    return rv


def assign(etfs, prev_sleeves):
    """etfs: code → row（含 etf_ok / etf_why / zone / val_code / value）。返回 code → (sleeve, why)。
    同一估值指数只留一只：已在长线仓的优先，其次持仓市值大的。"""
    out, taken = {}, {}
    order = sorted(etfs.values(), key=lambda r: ((prev_sleeves.get(r["code"]) or {}).get("sleeve") != "core",
                                                 -(r.get("value") or 0)))
    for r in order:
        c = r["code"]
        was_core = (prev_sleeves.get(c) or {}).get("sleeve") == "core"
        fam = r.get("val_code")
        if was_core:
            out[c] = ("core", "上次已归长线仓" + ("" if r.get("etf_ok") else "（本次未满足：" + "；".join(r.get("etf_why") or []) + "）"))
            if fam:
                taken.setdefault(fam, c)
        elif not r.get("etf_ok"):
            out[c] = ("trade", "未满足 ETF 长线条件：" + "；".join(r.get("etf_why") or ["无数据"]))
        elif fam and fam in taken:
            out[c] = ("trade", f"与 {taken[fam]} 同一估值指数（{fam}），长线仓只放一只")
        elif r.get("zone") in ("sell_half", "sell_all"):
            out[c] = ("trade", f"满足 ETF 长线条件，但估值偏高（空间 {r.get('upside_pct')}%），暂留交易仓，回到持有区后再转")
        elif r.get("zone") in ("buy", "hold"):
            out[c] = ("core", "满足 ETF 长线条件（指数估值法）")
            if fam:
                taken[fam] = c
        else:
            out[c] = ("trade", "无法定目标价：" + (r.get("why") or ""))
    return out
