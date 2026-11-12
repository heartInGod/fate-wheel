"""基金/ETF 类接口：东方财富基金净值、腾讯 ETF 实时溢价。"""
from ._http import DataSourceError, http_json, normalize, to_float
from .quotes import realtime_quotes


def etf_premium(codes):
    """ETF 实时溢价：现价、IOPV（实时参考净值）、上一日净值、溢价率（腾讯行情）。"""
    out = []
    for q in realtime_quotes(codes):
        if q.get("error"):
            out.append(q)
            continue
        if not q.get("is_etf"):
            continue
        prem_nav = None
        if q.get("price") and q.get("nav_prev"):
            prem_nav = round((q["price"] / q["nav_prev"] - 1) * 100, 2)
        out.append({"code": q["code"], "name": q["name"], "price": q["price"],
                    "iopv": q.get("iopv"), "premium_pct": q.get("premium_pct"),
                    "nav_prev": q.get("nav_prev"), "premium_vs_nav_pct": prem_nav,
                    "quote_time": q["quote_time"]})
    return out


def etf_nav_history(code, n=10):
    """场内/场外基金历史单位净值（东方财富）。QDII 净值一般滞后 1~2 个交易日。"""
    c = normalize(code)["code"]
    j = http_json("https://api.fund.eastmoney.com/f10/lsjz",
                  params={"fundCode": c, "pageIndex": 1, "pageSize": int(n)},
                  headers={"Referer": "https://fundf10.eastmoney.com/"})
    rows = ((j.get("Data") or {}).get("LSJZList")) or []
    if not rows:
        raise DataSourceError(f"{c} 无净值数据")
    return [{"date": r["FSRQ"], "nav": to_float(r["DWJZ"]), "acc_nav": to_float(r["LJJZ"]),
             "chg_pct": to_float(r["JZZZL"]), "subscribe": r.get("SGZT"), "redeem": r.get("SHZT")}
            for r in rows]
