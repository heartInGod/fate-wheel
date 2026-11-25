"""大盘资金面：市场级资金与宽度指标（个股视角的 capital.py 之外）。

与 market_pulse.py 的分工：
  market_pulse —— 宽基 ETF 量能/份额，识别“国家队护盘迹象”（点状、事件驱动）；
  market_flow  —— 全市场宽度、成交额、主力资金、两融、外盘（面状、每日背景）。

采集口径（全部为原始指标，不做因果判断；分档见 core/market_regime.py）：
  breadth   涨跌家数/平家数（东财 push2 ulist.np 单指数 f104/f105/f106，沪深各一次）
            涨跌停家数（东财 push2ex getTopicZTPool/DTPool 的 data.tc）
  turnover  沪深两市成交额（腾讯简版 s_sh000001/s_sz399001，单位万元→亿元）
            成交额近 250 日分位（腾讯日 K 的 amount，判断“年内新低”）
  mainflow  沪深指数主力净额（东财 push2his fflow/kline，f52 当日、近 20 日累计）
  margin    融资余额及连降天数（东财 datacenter RPTA_WZRQ_LSHJ，即市场融资历史）
  external  美元离岸人民币、伦敦金、WTI 原油、纳指100（新浪 hq，注意 gbk 编码）
  cb        央行公开市场净投放：无稳定公开接口，留空由模型用 WebSearch 补。

每个子项独立 try，失败写 errors 不影响整体；全部经 _http 限速/熔断。
当天结果缓存到 cache/market_flow_YYYYMMDD.json，refresh=1 强制重取。
"""
import datetime
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from ._http import DataSourceError, http_get, http_json, to_float

# market_flow.py 位于 <ROOT>/src/datasources/ 下，上溯三级即项目根
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CACHE_DIR = os.path.join(ROOT, "cache")
_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_ZT_UT = "7eea3edcaed734bea9cbfc24409ed989"   # push2ex 涨跌停池专用
_EM_QUOTE = {"Referer": "https://quote.eastmoney.com/"}
_EM_DATA = {"Referer": "https://data.eastmoney.com/"}   # push2his 资金流需此 Referer，否则被 WAF 断连
_SINA = {"Referer": "https://finance.sina.com.cn/"}


def _cache_path(day=None):
    d = day or datetime.date.today().isoformat()
    return os.path.join(CACHE_DIR, f"market_flow_{d.replace('-', '')}.json")


def load_cache(day=None):
    p = _cache_path(day)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return None


def _save_cache(obj, day=None):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_cache_path(day), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)


# ---------------- ① 市场宽度 ----------------
def _sina_breadth():
    """新浪 Market_Center 二分统计全市场涨跌家数（东财 push2 被 WAF 时的降级）。

    按 changepercent 降序（asc=0）分页：涨幅从 +20% 到 -20%。二分找出
    「页尾首次 <=0」的页 p0，则 1..p0 含全部上涨及部分平/跌，p0+1 起全为下跌。
    只精确统计 p0 与 p0+1 两页即可得出涨/平/跌，约 7~8 次请求。
    """
    import json as _json
    base = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData?page={p}&num=100&sort=changepercent&asc=0&node=hs_a")
    total_txt = http_get(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeStockCount?node=hs_a", headers=_SINA, retries=1).strip().strip('"')
    total = int(float(total_txt))
    pages = (total + 99) // 100

    def page(p):
        return _json.loads(http_get(base.format(p=p), headers=_SINA, retries=1))

    # 二分找第一个「页尾 <= 0」的页 p0（降序：涨幅从高到低）
    lo, hi = 1, pages
    while lo < hi:
        mid = (lo + hi) // 2
        rows = page(mid)
        tail = to_float(rows[-1].get("changepercent")) if rows else None
        if tail is None or tail > 0:
            lo = mid + 1
        else:
            hi = mid
    p0 = lo
    # p0 之前的页全部 >0；p0 页含涨跌分界。只统计 p0 前一页和 p0 页。
    up = (p0 - 1) * 100
    flat = down = 0
    for p in (p0 - 1, p0, p0 + 1):
        if p < 1 or p > pages:
            continue
        rows = page(p)
        vals = [to_float(r.get("changepercent")) for r in rows]
        vals = [v for v in vals if v is not None]
        if p < p0:
            continue                      # 已按整页计入 up
        for v in vals:
            if v > 0:
                up += 1
            elif v == 0:
                flat += 1
            else:
                down += 1
        if p == p0 + 1:
            break
    # p0+1 起的其余页全为下跌
    if p0 + 1 <= pages:
        down += max(0, total - (p0 + 1) * 100)
    return up, down, flat


def breadth():
    """涨跌家数（沪深）+ 涨跌停家数。返回 dict。"""
    out = {"up": None, "down": None, "flat": None, "up_ratio": None,
           "limit_up": None, "limit_down": None, "ud_ratio": None, "errors": []}

    def counts(secid, label):
        j = http_json(
            f"https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids={secid}"
            f"&fields=f104,f105,f106&ut={_UT}",
            headers=_EM_QUOTE, retries=1)
        d = ((j.get("data") or {}).get("diff") or [{}])[0]
        return to_float(d.get("f104")), to_float(d.get("f105")), to_float(d.get("f106"))

    up = down = flat = 0.0
    got = False
    # 主源：新浪 Market_Center 二分（稳定，约 10s；东财 push2 常被 WAF 断连）
    try:
        up, down, flat = _sina_breadth()
        got = True
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"涨跌家数(新浪): {e}")
    # 降级：东财 ulist.np（沪深各一次，retries=0 快速失败）
    if not got:
        try:
            u = d = f = 0.0
            for secid, label in (("1.000001", "沪"), ("0.399001", "深")):
                j = http_json(
                    f"https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids={secid}"
                    f"&fields=f104,f105,f106&ut={_UT}", headers=_EM_QUOTE, retries=0)
                row = ((j.get("data") or {}).get("diff") or [{}])[0]
                u += to_float(row.get("f104")) or 0
                d += to_float(row.get("f105")) or 0
                f += to_float(row.get("f106")) or 0
            up, down, flat = u, d, f
            got = True
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"涨跌家数(东财降级): {e}")
    if got:
        out.update(up=int(up), down=int(down), flat=int(flat))
        tot = up + down + flat
        out["up_ratio"] = round(up / tot * 100, 1) if tot else None

    # 涨跌停池：用今天日期；count 在 data.tc
    day = datetime.date.today().strftime("%Y%m%d")
    for key, url in (
        ("limit_up", "getTopicZTPool"), ("limit_down", "getTopicDTPool")
    ):
        try:
            j = http_json(
                f"https://push2ex.eastmoney.com/{url}?ut={_ZT_UT}&dpt=wz.ztzt"
                f"&Pageindex=0&pagesize=1&sort=fbt%3Aasc&date={day}",
                headers=_EM_QUOTE, retries=1)
            data = j.get("data") or {}
            tc = to_float(data.get("tc"))
            out[key] = int(tc) if tc is not None else None
            if j.get("rc") not in (0, None):
                out["errors"].append(f"{key}: rc={j.get('rc')}")
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{key}: {e}")
    if out["limit_up"] and out["limit_down"]:
        out["ud_ratio"] = round(out["limit_up"] / out["limit_down"], 2)
    return out


# ---------------- ② 成交额 ----------------
def turnover(count=250):
    """沪深两市当日成交额（亿元）+ 近 count 日分位。"""
    from datasources import quotes
    out = {"yi": None, "sh_yi": None, "sz_yi": None, "pct_rank": None,
           "low_yi": None, "high_yi": None, "errors": []}
    try:
        txt = http_get("https://qt.gtimg.cn/q=s_sh000001,s_sz399001",
                       encoding="gbk", headers=_SINA, retries=1)
        vals = {}
        for line in txt.split(";"):
            m = re.search(r'v_s_(sh000001|sz399001)="([^"]+)"', line)
            if m:
                parts = m.group(2).split("~")
                # 简版字段：...~成交量(手)~成交额(万元)~~总市值~...
                vals[m.group(1)] = to_float(parts[7]) if len(parts) > 7 else None
        sh = (vals.get("sh000001") or 0) / 1e4   # 万元 → 亿元
        sz = (vals.get("sz399001") or 0) / 1e4
        out.update(sh_yi=round(sh, 1), sz_yi=round(sz, 1), yi=round(sh + sz, 1))
        if sh <= 0 or sz <= 0:
            out["errors"].append("成交额解析异常（沪深有一项为空）")
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"成交额实时: {e}")
    try:
        # 指数成交额分位：用腾讯指数日 K（sh000001 第6字段=成交量/手，近似成交额趋势）。
        # 注意不能用 quotes.kline("000001")——那是平安银行个股，不是上证指数。
        import json as _json
        import bisect
        u = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
             f"?param=sh000001,day,,,{count},qfq")
        j = _json.loads(http_get(u, headers=_SINA, retries=1))
        rows = ((j.get("data") or {}).get("sh000001") or {}).get("day") or []
        vols = sorted(to_float(r[5]) for r in rows if len(r) > 5 and to_float(r[5]))
        cur = to_float(rows[-1][5]) if rows else None
        if vols and cur:
            out["pct_rank"] = round(bisect.bisect_left(vols, cur) / len(vols) * 100, 1)
            out["rank_basis"] = "上证指数近250日成交量分位（量能口径，近似成交额）"
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"成交额分位: {e}")
    return out


# ---------------- ③ 主力资金（全市场） ----------------
def main_flow(days=20):
    """沪深指数主力净流入：当日、近 days 日累计（亿元）。东财 fflow/kline 的 f52。"""
    out = {"today_yi": None, "cum_yi": None, "sh_today_yi": None, "sz_today_yi": None,
           "days": days, "errors": []}
    vals = {}
    for secid, key in (("1.000001", "sh"), ("0.399001", "sz")):
        try:
            j = http_json(
                f"https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
                f"?lmt={days + 5}&klt=101&ut={_UT}&secid={secid}"
                f"&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57",
                headers=_EM_DATA, retries=1)
            ks = ((j.get("data") or {}).get("klines") or [])
            if not ks:
                raise DataSourceError(f"{key} 无资金流数据")
            series = [(k.split(",")[0], to_float(k.split(",")[1])) for k in ks]
            series = [(d, v) for d, v in series if v is not None]
            vals[key] = series[-days:]
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"主力资金 {key}: {e}")
    if vals:
        # 按日期对齐求和（沪深日期应一致）
        by_date = {}
        for key, series in vals.items():
            for d, v in series:
                by_date.setdefault(d, {})[key] = v
        total = sum(sum(x.values()) for x in by_date.values())
        out["cum_yi"] = round(total / 1e8, 1)
        last = max(by_date) if by_date else None
        if last:
            out["today_yi"] = round(sum(by_date[last].values()) / 1e8, 1)
            out["sh_today_yi"] = round((by_date[last].get("sh") or 0) / 1e8, 1)
            out["sz_today_yi"] = round((by_date[last].get("sz") or 0) / 1e8, 1)
            out["as_of"] = last
    return out


# ---------------- ④ 融资余额 ----------------
def margin(days=10):
    """市场融资余额（亿元）与连降天数。东财 datacenter RPTA_WZRQ_LSHJ（市场融资历史）。"""
    out = {"balance_yi": None, "down_days": 0, "as_of": None, "errors": []}
    try:
        j = http_json(
            "https://datacenter-web.eastmoney.com/api/data/v1/get"
            "?reportName=RPTA_RZRQ_LSHJ&columns=DIM_DATE,RZYE,RZCHE"
            "&sortColumns=DIM_DATE&sortTypes=-1"
            f"&pageSize={days}&pageNumber=1",
            headers={"Referer": "https://data.eastmoney.com/"}, retries=1)
        rows = ((j.get("result") or {}).get("data") or [])
        series = [(str(x.get("DIM_DATE"))[:10], to_float(x.get("RZYE"))) for x in rows]
        series = [(d, v) for d, v in series if v is not None]
        if not series:
            raise DataSourceError("融资历史为空")
        out["balance_yi"] = round(series[0][1] / 1e8, 1)
        out["as_of"] = series[0][0]
        # series 已按日期倒序（新→旧），连降 = 从最新开始逐日走低
        n = 0
        for i in range(1, len(series)):
            if series[i - 1][1] < series[i][1]:   # 新的一天 < 前一天 → 又降一天
                n += 1
            else:
                break
        out["down_days"] = n
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"融资余额: {e}")
    return out


# ---------------- ⑤ 外盘 ----------------
def external():
    """美元离岸人民币、伦敦金、WTI 原油、纳指100。新浪 hq（gbk）。"""
    out = {"usdcnh": None, "gold": None, "oil": None, "nasdaq100": None, "errors": []}
    try:
        txt = http_get("https://hq.sinajs.cn/list=fx_susdcnh,hf_GC,hf_CL,gb_$ndx",
                       encoding="gbk", headers=_SINA, retries=1)
        def field(name, idx):
            m = re.search(r'v_hq_str_?' + re.escape(name) + r'="([^"]*)"', txt)
            if not m:
                m = re.search(re.escape(name) + r'="([^"]*)"', txt)
            if not m:
                return None
            parts = m.group(1).split(",")
            return to_float(parts[idx]) if len(parts) > idx else None
        out["usdcnh"] = field("fx_susdcnh", 1)
        out["gold"] = field("hf_GC", 0)
        out["oil"] = field("hf_CL", 0)
        out["nasdaq100"] = field("gb_$ndx", 1)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"外盘: {e}")
    return out


# ---------------- 汇总 ----------------
def collect(refresh=False, day=None):
    """采集全部市场级指标；优先读当天缓存，refresh=1 强制重取。"""
    cached = load_cache(day)
    if cached and not refresh:
        return cached
    tasks = {
        "breadth": breadth,
        "turnover": turnover,
        "main_flow": main_flow,
        "margin": margin,
        "external": external,
    }
    out = {"generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futs = {k: ex.submit(fn) for k, fn in tasks.items()}
        for k, fut in futs.items():
            try:
                out[k] = fut.result()
            except Exception as e:  # noqa: BLE001
                out[k] = {"errors": [f"{k}: {type(e).__name__}: {e}"]}
    out["errors"] = []
    for k, v in out.items():
        if isinstance(v, dict) and v.get("errors"):
            out["errors"].extend(f"{k}: {e}" for e in v["errors"])
    _save_cache(out, day)
    return out


if __name__ == "__main__":
    import sys
    refresh = "refresh" in sys.argv
    print(json.dumps(collect(refresh=refresh), ensure_ascii=False, indent=1, default=str))
