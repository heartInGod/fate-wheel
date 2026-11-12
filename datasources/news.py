"""新闻类接口：东方财富资讯搜索（个股新闻）、新浪财经 7x24 快讯。"""
import json
import re

from ._http import http_json


def _clean(s):
    return re.sub(r"<[^>]+>", "", s or "").replace("&nbsp;", " ").strip()


def stock_news(keyword, n=10):
    """按关键词（股票名称/代码/主题）搜索最新财经新闻，东方财富资讯，按时间倒序。"""
    param = {"uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"], "client": "web",
             "clientType": "web", "clientVersion": "curr",
             "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                            "pageIndex": 1, "pageSize": int(n), "preTag": "", "postTag": ""}}}
    j = http_json("https://search-api-web.eastmoney.com/search/jsonp",
                  params={"cb": "cb", "param": json.dumps(param, ensure_ascii=False)})
    rows = (j.get("result") or {}).get("cmsArticleWebOld") or []
    return [{"date": r.get("date"), "title": _clean(r.get("title")), "media": r.get("mediaName"),
             "summary": _clean(r.get("content"))[:120], "url": r.get("url")} for r in rows]


def flash_news(n=30, keyword=None):
    """新浪财经 7x24 全球快讯；keyword 可选，按关键词过滤（如 纳指、半导体、美联储）。"""
    j = http_json("https://zhibo.sina.com.cn/api/zhibo/feed",
                  params={"page": 1, "page_size": int(n), "zhibo_id": 152, "type": 0})
    rows = (((j.get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or []
    out = [{"time": r.get("create_time"), "text": _clean(r.get("rich_text"))} for r in rows]
    if keyword:
        out = [r for r in out if keyword in r["text"]]
    return out


# 政策 / 国家队相关快讯。强关键词命中即收录；弱关键词（监管部门名）需同时提到股市相关词，过滤掉逆回购等日常操作
POLICY_STRONG = ("汇金", "国家队", "证金公司", "中证金融", "国新", "诚通", "平准", "护盘", "稳市", "稳定市场", "稳预期",
                 "增持再贷款", "回购增持", "一行一局一会", "一行两会", "降准", "降息", "印花税",
                 "中长期资金入市", "长期资金入市", "险资入市", "社保基金", "养老金入市", "举牌")
POLICY_WEAK = ("证监会", "央行", "人民银行", "金融监管总局", "国务院", "财政部", "政治局", "国常会")
POLICY_MARKET = ("股市", "资本市场", "A股", "股票", "ETF", "权益", "投资者", "融资融券", "互换便利", "回购增持")
# 个股事务 / 境外央行，命中即排除
POLICY_EXCLUDE = ("划转", "定增", "募资", "风险警示", "异常波动", "公司债", "摘牌", "做市",
                  "欧洲央行", "日本央行", "英国央行", "美联储", "澳洲联储", "加拿大央行", "瑞士央行")


def _headline(text):
    """取【】里的标题；没有标题时取前 80 字。只在标题上匹配，避免正文里的“保证金”“印花税”之类误命中。"""
    if text.startswith("【") and "】" in text:
        return text[1:text.index("】")]
    return text[:80]


def _policy_hit(text):
    h = _headline(text)
    if any(x in h for x in POLICY_EXCLUDE):
        return []
    hits = [k for k in POLICY_STRONG if k in h]
    if not hits:
        weak = [k for k in POLICY_WEAK if k in h]
        if weak and any(m in h for m in POLICY_MARKET):
            hits = weak
    return hits


def policy_news(since, max_pages=25, page_size=100):
    """新浪 7x24 快讯里 since（'yyyy-mm-dd HH:MM:SS'）之后的政策 / 国家队相关条目，按时间倒序。
    每条带 keywords（命中的关键词）。翻页直到早于 since 或达到 max_pages。"""
    out, seen = [], set()
    for page in range(1, int(max_pages) + 1):
        j = http_json("https://zhibo.sina.com.cn/api/zhibo/feed",
                      params={"page": page, "page_size": int(page_size), "zhibo_id": 152, "type": 0})
        rows = (((j.get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or []
        if not rows:
            break
        for r in rows:
            t = r.get("create_time") or ""
            if t < since:
                continue
            text = _clean(r.get("rich_text"))
            hits = _policy_hit(text)
            key = text[:60]
            if hits and key not in seen:
                seen.add(key)
                out.append({"time": t, "keywords": hits, "text": text[:300]})
        if (rows[-1].get("create_time") or "") < since:
            break
    return out
