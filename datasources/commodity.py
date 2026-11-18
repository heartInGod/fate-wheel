"""商品 / 行业价格：给定位前提（thesis.json 里 kind=商品价格 的判断，如「钨价下行」）提供可核对的数据。

品种 id → 数据源（SERIES）：
  钨：中钨在线 ctia.com.cn 每日钨市日评（WordPress 接口，交易日约 14:00 发布，正文解析 65% 钨精矿 / APT 报价）
  国内期货（主力连续）：新浪 InnerFuturesNewService 日 K + hq.sinajs.cn 实时价（日 K 只到上一交易日，当天用实时价补）
  外盘期货：新浪 GlobalFuturesService 日 K（含当天）
  BDI：东方财富数据中心 RPT_INDUSTRY_INDEX
持仓关联哪些品种写在 thesis.json 每只标的的 commodities 字段（`python3 main.py thesis link 代码 品种...`）。
输出统一为：最新值、日期、单位、1 日 / 1 周（7 天前）/ 1 月（30 天前）变化 %、近 30 天高低点、最近 6 个点。
"""
import datetime
import json
import os
import re

from ._http import DataSourceError, http_get, http_json, to_float

from paths import ROOT
CACHE = os.path.join(ROOT, "cache", "commodity.json")

SINA_H = {"Referer": "https://finance.sina.com.cn/"}

# id: (名称, 单位, 源, 源参数)
SERIES = {
    "W_ORE": ("钨精矿（65%，国内）", "万元/标吨", "ctia", "ore"),
    "APT": ("仲钨酸铵 APT（国内）", "万元/吨", "ctia", "apt"),
    "AL": ("沪铝连续", "元/吨", "sina_inner", "AL0"),
    "CU": ("沪铜连续", "元/吨", "sina_inner", "CU0"),
    "SC": ("上海原油连续", "元/桶", "sina_inner", "SC0"),
    "LC": ("碳酸锂连续", "元/吨", "sina_inner", "LC0"),
    "EC": ("集运指数（欧线）连续", "点", "sina_inner", "EC0"),
    "AU": ("沪金连续", "元/克", "sina_inner", "AU0"),
    "BRENT": ("布伦特原油", "美元/桶", "sina_global", "OIL"),
    "WTI": ("WTI 原油", "美元/桶", "sina_global", "CL"),
    "NG": ("NYMEX 天然气", "美元/百万英热", "sina_global", "NG"),
    "BDI": ("波罗的海干散货指数 BDI", "点", "em_index", "EMI00107664"),
}


# ---------------- 各数据源：返回 [(date 'YYYY-MM-DD', value)] 升序 ----------------

def _jsonp_list(text):
    m = re.search(r"\((\[.*\])\)", text, re.S)
    if not m:
        raise DataSourceError(f"新浪期货返回异常: {text[:120]}")
    return json.loads(m.group(1))


def _sina_inner(sym, rt=None):
    rows = _jsonp_list(http_get(
        f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{sym}=/InnerFuturesNewService.getDailyKLine",
        params={"symbol": sym}, headers=SINA_H))
    pts = [(r["d"][:10], to_float(r["c"])) for r in rows[-80:] if to_float(r.get("c"))]
    q = (rt or {}).get(sym)
    if q and q[1] and (not pts or q[0] > pts[-1][0]):
        pts.append(q)      # 当天（含夜盘）用实时价补上
    return pts


def _sina_inner_rt(syms):
    """hq.sinajs.cn 内盘期货实时：sym → (date, last)。字段：8=最新价，17=日期。"""
    if not syms:
        return {}
    text = http_get("https://hq.sinajs.cn/list=" + ",".join("nf_" + s for s in syms), headers=SINA_H, encoding="gbk")
    out = {}
    for sym, body in re.findall(r'hq_str_nf_(\w+)="([^"]*)"', text):
        f = body.split(",")
        if len(f) > 17 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", f[17]) and to_float(f[8]):
            out[sym] = (f[17], to_float(f[8]))
    return out


def _sina_global(sym):
    rows = _jsonp_list(http_get(
        f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{sym}=/GlobalFuturesService.getGlobalFuturesDailyKLine",
        params={"symbol": sym}, headers=SINA_H))
    return [(r["date"][:10], to_float(r["close"])) for r in rows[-80:] if to_float(r.get("close"))]


def _em_index(ind):
    j = http_json("https://datacenter-web.eastmoney.com/api/data/v1/get", params={
        "reportName": "RPT_INDUSTRY_INDEX", "columns": "REPORT_DATE,INDICATOR_VALUE,CHANGE_RATE",
        "filter": f'(INDICATOR_ID="{ind}")', "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        "pageSize": "60", "pageNumber": "1", "source": "WEB", "client": "WEB"})
    data = ((j or {}).get("result") or {}).get("data") or []
    pts = sorted((r["REPORT_DATE"][:10], to_float(r["INDICATOR_VALUE"])) for r in data if to_float(r.get("INDICATOR_VALUE")))
    # 东财会在当天指数未公布时先放一条「同值、涨跌 0」的占位，去掉
    if len(pts) >= 2 and pts[-1][1] == pts[-2][1]:
        last = next((r for r in data if r["REPORT_DATE"][:10] == pts[-1][0]), {})
        if not to_float(last.get("CHANGE_RATE")):
            pts.pop()
    return pts


# 钨：从日评正文抽报价。句内排除：均价 / 预计 / 有望 / 欧洲 / 折合 / 同比；数字所在分句排除关口类描述。
_CTIA_SKIP_SENT = re.compile(r"均价|预计|有望|欧洲|折合|同比|环比|月初|月末|回看|此前|一路|攀升至|从约|历史")
_CTIA_SKIP_CLAUSE = re.compile(r"失守|击穿|关口|防线|万关|以下|上方|下方|心理|心里|试探|探索|重返|突破|跌破|站上|向\d")
_NUM = r"(\d+(?:\.\d+)?)(?:\s*[-—–~]\s*(\d+(?:\.\d+)?))?\s*万元/(标)?吨"


def _ctia_parse(html):
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    got = {}
    for sent in re.split(r"[。；;]", text):
        if _CTIA_SKIP_SENT.search(sent):
            continue
        for m in re.finditer(_NUM, sent):
            before = sent[:m.start()]
            clause = re.split(r"[，,]", before)[-1] + m.group(0) + re.split(r"[，,]", sent[m.end():])[0]
            if _CTIA_SKIP_CLAUSE.search(clause):
                continue
            # 数字前最近出现的品种关键词决定归属
            cands = [(before.rfind(k), key) for k, key in (("APT", "apt"), ("仲钨酸铵", "apt"), ("钨精矿", "ore"),
                                                         ("钨粉", "x"), ("碳化钨", "x"), ("钨铁", "x"), ("钴", "x"),
                                                         ("钨废", "x"), ("废钨", "x"))]
            pos, key = max(cands)
            if pos < 0 or key == "x" or key in got:
                continue
            lo = float(m.group(1))
            hi = float(m.group(2)) if m.group(2) else lo
            if hi < lo or hi > lo * 1.3:
                continue
            got[key] = round((lo + hi) / 2, 2)
    return got


def _ctia(kind, _cache={}):
    if "rows" not in _cache:
        posts = http_json("http://www.ctia.com.cn/wp-json/wp/v2/posts", timeout=15, params={
            "search": "钨制品价格走势", "per_page": "45", "_fields": "date,title,content"})
        rows = {}
        for p in posts:
            d = (p.get("date") or "")[:10]
            if "全图" in ((p.get("title") or {}).get("rendered") or ""):
                continue      # 月度 / 季度汇总稿，正文多为均价和回顾，不当作当天报价
            if d and d not in rows:
                rows[d] = _ctia_parse((p.get("content") or {}).get("rendered") or "")
        _cache["rows"] = rows
    pts = sorted((d, v[kind]) for d, v in _cache["rows"].items() if kind in v)
    return _drop_outliers(pts)


def _drop_outliers(pts, tol=0.2):
    """文本解析的兜底：与前后相邻点都偏离超过 tol 的孤立点视为误抽，丢弃（真实行情是连续变化的）。"""
    keep = []
    for i, (d, v) in enumerate(pts):
        nb = [pts[j][1] for j in (i - 1, i + 1) if 0 <= j < len(pts)]
        if nb and all(abs(v / x - 1) > tol for x in nb):
            continue
        keep.append((d, v))
    return keep


# ---------------- 汇总 ----------------

def _at(pts, day):
    """day 当天或之前最近一个点的值。"""
    v = None
    for d, x in pts:
        if d <= day:
            v = x
        else:
            break
    return v


def _pct(a, b):
    return round((a / b - 1) * 100, 2) if a and b else None


def view(sid, pts):
    name, unit, src, _ = SERIES[sid]
    if not pts:
        return {"id": sid, "name": name, "unit": unit, "source": src, "error": "无数据"}
    last_d, last = pts[-1]
    ld = datetime.date.fromisoformat(last_d)
    w = _at(pts, (ld - datetime.timedelta(days=7)).isoformat())
    mo = _at(pts, (ld - datetime.timedelta(days=30)).isoformat())
    m_pts = [x for d, x in pts if d >= (ld - datetime.timedelta(days=30)).isoformat()]
    return {"id": sid, "name": name, "unit": unit, "source": src, "date": last_d, "last": last,
            "chg_1d": _pct(last, pts[-2][1]) if len(pts) > 1 else None,
            "chg_1w": _pct(last, w), "chg_1m": _pct(last, mo),
            "hi_1m": max(m_pts), "lo_1m": min(m_pts),
            "recent": [[d[5:], x] for d, x in pts[-6:]],
            "age_days": (datetime.date.today() - ld).days}


def prices(ids=None):
    """品种价格及变化。ids 为空时返回全部品种。单个品种失败只在该项写 error。"""
    ids = [i.upper() for i in (ids or SERIES)]
    bad = [i for i in ids if i not in SERIES]
    if bad:
        raise DataSourceError(f"未知品种 {bad}，可选：{', '.join(SERIES)}")
    rt = {}
    inner = [SERIES[i][3] for i in ids if SERIES[i][2] == "sina_inner"]
    try:
        rt = _sina_inner_rt(inner)
    except Exception:  # noqa: BLE001  实时价失败时只用日 K
        rt = {}
    out = []
    for i in ids:
        _, _, src, arg = SERIES[i]
        try:
            pts = {"ctia": lambda: _ctia(arg), "sina_inner": lambda: _sina_inner(arg, rt),
                   "sina_global": lambda: _sina_global(arg), "em_index": lambda: _em_index(arg)}[src]()
            out.append(view(i, pts))
        except Exception as e:  # noqa: BLE001
            out.append({"id": i, "name": SERIES[i][0], "unit": SERIES[i][1], "source": src, "error": str(e)[:160]})
    _save(out)
    return out


def _save(rows):
    """按品种合并写 cache/commodity.json（看板离线读取），失败不影响返回。"""
    try:
        old = load_cache()
        old.update({r["id"]: r for r in rows if not r.get("error")})
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False)
        os.replace(tmp, CACHE)
    except OSError:
        pass


def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def series_list():
    return [{"id": k, "name": v[0], "unit": v[1], "source": v[2]} for k, v in SERIES.items()]
