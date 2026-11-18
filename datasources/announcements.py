"""公告类接口：巨潮资讯（A股官方）、港交所披露易（港股官方）、东方财富公告（兜底）。"""
import datetime
import html

from ._http import DataSourceError, http_json, normalize


def _date_range(days):
    end = datetime.date.today()
    return end - datetime.timedelta(days=int(days)), end


def cninfo_announcements(code, days=7, n=20):
    """A股公告，巨潮资讯（证监会指定披露平台）。"""
    c = normalize(code)
    if c["market"] == "hk":
        raise DataSourceError("巨潮资讯仅支持 A 股")
    info = http_json("http://www.cninfo.com.cn/new/information/topSearch/query",
                     data={"keyWord": c["code"], "maxNum": 5})
    hit = next((x for x in info if x.get("code") == c["code"]), None)
    if not hit or not hit.get("orgId"):
        raise DataSourceError(f"巨潮未找到 {c['code']} 的 orgId")
    org = hit["orgId"]
    start, end = _date_range(days)
    sh = c["market"] == "sh"
    if hit.get("type") == "fund":  # 场内基金/ETF 公告在巨潮的 fund 栏目，不分沪深板块
        column, plate = "fund", ""
    else:
        column, plate = ("sse", "sh") if sh else ("szse", "sz")
    j = http_json("http://www.cninfo.com.cn/new/hisAnnouncement/query", data={
        "stock": f"{c['code']},{org}", "tabName": "fulltext", "pageSize": int(n), "pageNum": 1,
        "column": column, "plate": plate,
        "seDate": f"{start}~{end}", "category": "", "searchkey": "", "secid": "",
        "sortName": "", "sortType": "", "isHLtitle": "true"})
    out = []
    for a in j.get("announcements") or []:
        t = datetime.datetime.fromtimestamp(a["announcementTime"] / 1000)
        out.append({"code": c["code"], "name": a.get("secName"), "date": t.strftime("%Y-%m-%d"),
                    "title": (a.get("announcementTitle") or "").replace("<em>", "").replace("</em>", ""),
                    "url": "http://static.cninfo.com.cn/" + a.get("adjunctUrl", ""), "source": "巨潮资讯"})
    return out


def hkex_announcements(code, days=14, n=20):
    """港股公告，港交所披露易（官方）。"""
    c = normalize(code)
    if c["market"] != "hk":
        raise DataSourceError("披露易仅支持港股")
    info = http_json("https://www1.hkexnews.hk/search/prefix.do",
                     params={"callback": "callback", "lang": "ZH", "type": "A", "name": c["code"], "market": "SEHK"})
    sid = next((x["stockId"] for x in info.get("stockInfo", []) if x.get("code") == c["code"]), None)
    if not sid:
        raise DataSourceError(f"披露易未找到 {c['code']}")
    start, end = _date_range(days)
    j = http_json("https://www1.hkexnews.hk/search/titleSearchServlet.do", params={
        "sortDir": 0, "sortByOptions": "DateTime", "category": 0, "market": "SEHK", "stockId": sid,
        "documentType": -1, "fromDate": start.strftime("%Y%m%d"), "toDate": end.strftime("%Y%m%d"),
        "title": "", "searchType": 0, "t1code": -2, "t2Gcode": -2, "t2code": -2,
        "rowRange": int(n), "lang": "zh"})
    import json as _json
    rows = _json.loads(j.get("result") or "[]") if isinstance(j.get("result"), str) else []
    out = []
    for r in rows:
        d, _, tm = r.get("DATE_TIME", "").partition(" ")
        dd, mm, yy = (d.split("/") + ["", "", ""])[:3]
        out.append({"code": c["code"], "name": r.get("STOCK_NAME"), "date": f"{yy}-{mm}-{dd} {tm}".strip(),
                    "title": html.unescape(r.get("TITLE") or "").replace("\n", " "),
                    "category": html.unescape((r.get("LONG_TEXT") or "").replace("<br/>", "")),
                    "url": "https://www1.hkexnews.hk" + r.get("FILE_LINK", ""), "source": "披露易"})
    return out


_FUND_ANN_CAT = {"1": "发行运作", "2": "分红送配", "3": "定期报告", "4": "人事调整", "5": "基金销售", "6": "其他"}


def _em_fund_announcements(c, n):
    j = http_json("https://api.fund.eastmoney.com/f10/JJGG", headers={"Referer": "https://fundf10.eastmoney.com/"},
                  params={"fundcode": c, "pageIndex": 1, "pageSize": int(n), "type": 0})
    return [{"code": c, "name": a.get("ShortTitle"), "date": a.get("PUBLISHDATEDesc") or (a.get("PUBLISHDATE") or "")[:10],
             "title": a.get("TITLE"), "category": _FUND_ANN_CAT.get(str(a.get("NEWCATEGORY")), ""),
             "url": f"https://fund.eastmoney.com/gonggao/{c},{a.get('ID')}.html", "source": "天天基金"}
            for a in j.get("Data") or []]


def em_announcements(code, n=10):
    """A股/港股/基金公告，东方财富（聚合源，作兜底；基金走天天基金公告）。"""
    c = normalize(code)
    from .profile import is_fund
    if is_fund(code):
        return _em_fund_announcements(c["code"], n)
    j = http_json("https://np-anotice-stock.eastmoney.com/api/security/ann", params={
        "sr": -1, "page_size": int(n), "page_index": 1, "client_source": "web",
        "ann_type": "H" if c["market"] == "hk" else "A", "stock_list": c["code"]})
    out = []
    for a in (j.get("data") or {}).get("list") or []:
        cols = ",".join(x.get("column_name", "") for x in a.get("columns") or [])
        out.append({"code": c["code"], "name": (a.get("codes") or [{}])[0].get("short_name"),
                    "date": (a.get("notice_date") or "")[:10], "title": a.get("title"), "category": cols,
                    "url": f"https://data.eastmoney.com/notices/detail/{c['code']}/{a.get('art_code')}.html",
                    "source": "东方财富"})
    return out


def announcements(code, days=7, n=20):
    """通用公告接口：A股走巨潮、港股走披露易；官方源失败时自动回退东方财富。"""
    c = normalize(code)
    try:
        if c["market"] == "hk":
            return hkex_announcements(code, days=days, n=n)
        return cninfo_announcements(code, days=days, n=n)
    except DataSourceError:
        start, _ = _date_range(days)
        return [a for a in em_announcements(code, n=n) if a["date"] >= str(start)]
