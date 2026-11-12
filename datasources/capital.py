"""资金面接口：东方财富个股资金流、融资融券、南向/北向资金、板块资金流。"""
from ._http import DataSourceError, http_json, normalize, to_float

_FLOW_FIELDS = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"


def _flow_row(d):
    return {"code": d.get("f12"), "name": d.get("f14"), "price": to_float(d.get("f2")), "pct": to_float(d.get("f3")),
            "main_net": to_float(d.get("f62")), "main_net_pct": to_float(d.get("f184")),
            "super_net": to_float(d.get("f66")), "big_net": to_float(d.get("f72")),
            "mid_net": to_float(d.get("f78")), "small_net": to_float(d.get("f84"))}


def fund_flow_realtime(codes):
    """个股/ETF 当日实时资金流（单位元），可批量。东方财富优先，失败时 A股/ETF 逐只回退新浪当日数据。"""
    try:
        return em_fund_flow_realtime(codes)
    except DataSourceError:
        out = []
        for c in codes:
            n = normalize(c)
            if n["market"] == "hk":
                continue
            try:
                r = sina_fund_flow_daily(c, 1)[0]
            except DataSourceError:
                continue
            out.append({"code": n["code"], "date": r["date"], "price": r["close"], "pct": r["pct"],
                        "main_net": r["main_net"], "main_net_pct": r["main_net_pct"],
                        "super_net": r["super_net"], "source": "新浪"})
        if not out:
            raise
        return out


def em_fund_flow_realtime(codes):
    """个股/ETF 当日实时资金流，东方财富（主力=超大单+大单，单位元），批量。"""
    norms = [normalize(c) for c in codes]
    j = http_json("https://push2.eastmoney.com/api/qt/ulist.np/get", params={
        "fltt": 2, "secids": ",".join(n["secid"] for n in norms), "fields": _FLOW_FIELDS})
    rows = (j.get("data") or {}).get("diff") or []
    if not rows:
        raise DataSourceError("东方财富实时资金流为空")
    return [dict(_flow_row(d), source="东方财富") for d in rows]


SINA_MF = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"


def fund_flow_daily(code, days=10):
    """个股/ETF 近 N 日资金流向（单位元）。东方财富优先，失败回退新浪。"""
    try:
        return em_fund_flow_daily(code, days)
    except DataSourceError as e:
        if normalize(code)["market"] == "hk":
            raise
        try:
            return sina_fund_flow_daily(code, days)
        except DataSourceError as e2:
            raise DataSourceError(f"东方财富: {e}; 新浪: {e2}") from e2


def sina_fund_flow_daily(code, days=10):
    """个股/ETF 近 N 日资金流向，新浪财经（net=全部净流入，super=特大单净额）。"""
    n = normalize(code)
    if n["market"] == "hk":
        raise DataSourceError("新浪资金流不支持港股")
    rows = http_json(SINA_MF + "MoneyFlow.ssl_qsfx_zjlrqs", params={
        "page": 1, "num": int(days), "sort": "opendate", "asc": 0, "daima": n["tencent"]}) or []
    if not rows:
        raise DataSourceError(f"{code} 新浪无资金流数据")
    return [{"date": r["opendate"], "main_net": to_float(r.get("netamount")),
             "main_net_pct": round(to_float(r.get("ratioamount")) * 100, 2) if r.get("ratioamount") else None,
             "super_net": to_float(r.get("r0_net")), "close": to_float(r.get("trade")),
             "pct": round(to_float(r.get("changeratio")) * 100, 2) if r.get("changeratio") else None,
             "source": "新浪"} for r in rows]


def em_fund_flow_daily(code, days=10):
    """个股/ETF 近 N 日主力资金流向，东方财富（单位元）。"""
    n = normalize(code)
    j = http_json("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get", params={
        "lmt": int(days), "klt": 101, "secid": n["secid"], "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"})
    lines = (j.get("data") or {}).get("klines") or []
    if not lines:
        raise DataSourceError(f"{code} 无资金流数据")
    out = []
    for ln in lines:
        p = ln.split(",")
        out.append({"date": p[0], "main_net": to_float(p[1]), "small_net": to_float(p[2]),
                    "mid_net": to_float(p[3]), "big_net": to_float(p[4]), "super_net": to_float(p[5]),
                    "main_net_pct": to_float(p[6]), "close": to_float(p[11]), "pct": to_float(p[12]),
                    "source": "东方财富"})
    return out


def margin(code, days=5):
    """融资融券明细（沪深两融标的，T+1 更新，单位元/股）。"""
    n = normalize(code)
    if n["market"] == "hk":
        raise DataSourceError("港股无两融数据")
    j = http_json("https://datacenter-web.eastmoney.com/api/data/v1/get", params={
        "reportName": "RPTA_WEB_RZRQ_GGMX", "columns": "ALL", "source": "WEB",
        "sortColumns": "date", "sortTypes": -1, "pageNumber": 1, "pageSize": int(days),
        "filter": f'(scode="{n["code"]}")'})
    rows = ((j.get("result") or {}).get("data")) or []
    if not rows:
        raise DataSourceError(f"{n['code']} 不是两融标的或无数据")
    return [{"date": r["DATE"][:10], "rz_balance": r.get("RZYE"), "rz_buy": r.get("RZMRE"),
             "rz_repay": r.get("RZCHE"), "rz_net": r.get("RZJME"), "rz_net_5d": r.get("RZJME5D"),
             "rz_net_10d": r.get("RZJME10D"), "rq_balance": r.get("RQYE"), "rq_sell_vol": r.get("RQMCL"),
             "rzrq_balance": r.get("RZRQYE"), "rz_balance_to_float_pct": r.get("RZYEZB")} for r in rows]


def connect_flow():
    """沪深港通当日资金（单位万元）。南向=港股通（沪/深→港），北向自 2024 年起不再披露实时净买入。"""
    j = http_json("https://push2.eastmoney.com/api/qt/kamt/get",
                  params={"fields1": "f1,f2,f3,f4", "fields2": "f51,f52,f53,f54,f63"})
    d = j.get("data") or {}
    names = {"sh2hk": "港股通(沪)", "sz2hk": "港股通(深)", "hk2sh": "沪股通", "hk2sz": "深股通"}
    out = []
    for k, label in names.items():
        v = d.get(k) or {}
        out.append({"channel": label, "direction": "南向" if k.endswith("hk") else "北向",
                    "net_buy_wan": v.get("netBuyAmt"), "quota_remain_wan": v.get("dayAmtRemain"),
                    "quota_wan": v.get("dayAmtThreshold"), "status": v.get("status")})
    south = sum((x["net_buy_wan"] or 0) for x in out if x["direction"] == "南向")
    out.append({"channel": "南向合计", "direction": "南向", "net_buy_wan": round(south, 2)})
    return out


def sector_flow(kind="industry", top=10, ascending=False):
    """行业/概念板块资金流排行。东方财富优先，失败回退新浪。ascending=True 看净流出榜。"""
    try:
        return em_sector_flow(kind, top, ascending)
    except DataSourceError as e:
        try:
            return sina_sector_flow(kind, top, ascending)
        except DataSourceError as e2:
            raise DataSourceError(f"东方财富: {e}; 新浪: {e2}") from e2


def sina_sector_flow(kind="industry", top=10, ascending=False):
    """行业/概念板块资金流排行，新浪财经（含板块领涨股）。"""
    fen = {"industry": 0, "concept": 1}.get(kind)
    if fen is None:
        raise DataSourceError("kind 只能是 industry 或 concept")
    rows = http_json(SINA_MF + "MoneyFlow.ssl_bkzj_bk", params={
        "page": 1, "num": int(top), "sort": "netamount", "asc": 1 if ascending else 0, "fenlei": fen}) or []
    return [{"code": r.get("category"), "name": r.get("name"),
             "pct": round(to_float(r.get("avg_changeratio")) * 100, 2),
             "main_net": to_float(r.get("netamount")),
             "main_net_pct": round(to_float(r.get("ratioamount")) * 100, 2),
             "leader": r.get("ts_name"), "leader_pct": round(to_float(r.get("ts_changeratio")) * 100, 2),
             "source": "新浪"} for r in rows]


def em_sector_flow(kind="industry", top=10, ascending=False):
    """行业/概念板块主力资金流排行，东方财富。"""
    fs = {"industry": "m:90+t:2", "concept": "m:90+t:3"}.get(kind)
    if not fs:
        raise DataSourceError("kind 只能是 industry 或 concept")
    j = http_json("https://push2.eastmoney.com/api/qt/clist/get", params={
        "pn": 1, "pz": int(top), "po": 0 if ascending else 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f62", "fs": fs, "fields": _FLOW_FIELDS})
    rows = (j.get("data") or {}).get("diff") or []
    if not rows:
        raise DataSourceError("东方财富板块资金流为空")
    return [dict(_flow_row(d), source="东方财富") for d in rows]
