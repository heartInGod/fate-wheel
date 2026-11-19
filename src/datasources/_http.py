"""HTTP 与代码规范化等公共工具。"""
import gzip
import json
import random
import re
import zlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import logroll
except ImportError:
    logroll = None

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"


class DataSourceError(Exception):
    pass


# 按站点限速：东方财富、腾讯等短时间高频请求会被断开连接或被 WAF 拦截（返回 501，封 IP 一段时间）。
# 每个站点（同一主域的子域名共用一个节奏）：两次请求至少间隔 基础间隔 + 随机抖动 秒；
# 每连续请求 3 次再额外歇 1~3 秒（随机），避开按分钟计数的限流。多线程按预约时间片排队，锁外 sleep。
_PACE = {  # 主域: (基础间隔, 随机抖动上限)
    "eastmoney.com": (0.4, 0.25),
    "gtimg.cn": (0.25, 0.35), "qq.com": (0.25, 0.35),   # 腾讯行情 / 日 K
    "sina.com.cn": (0.3, 0.3), "sina.cn": (0.3, 0.3),
    "hkexnews.hk": (0.3, 0.3), "cninfo.com.cn": (0.2, 0.3),
    "csindex.com.cn": (0.3, 0.3), "sse.com.cn": (0.3, 0.3), "danjuanfunds.com": (0.3, 0.3),
}
_GROUP = {"qq.com": "tencent", "gtimg.cn": "tencent", "sina.cn": "sina", "sina.com.cn": "sina"}  # 同一家公司合并计数
_DEFAULT_PACE = (0.1, 0.2)
_BURST = (3, 3)            # 连续请求多少次后歇一下（随机取值，上下限相同即固定次数）
_BURST_PAUSE = (1.0, 3.0)  # 歇多久（秒，随机）
_next_at, _count, _burst_at = {}, {}, {}
_lock = threading.Lock()


def _throttle(url):
    host = urllib.parse.urlparse(url).netloc.split(":")[0]
    dom = next((d for d in _PACE if host == d or host.endswith("." + d)), None)
    gap, jit = _PACE.get(dom, _DEFAULT_PACE)
    key = _GROUP.get(dom, dom or host)
    with _lock:
        now = time.time()
        slot = max(now, _next_at.get(key, 0.0))
        n = _count.get(key, 0) + 1
        if n >= _burst_at.setdefault(key, random.randint(*_BURST)):
            n, _burst_at[key] = 0, random.randint(*_BURST)
            slot += random.uniform(*_BURST_PAUSE)
        _count[key] = n
        _next_at[key] = slot + gap + random.uniform(0, jit)
    if slot > now:
        wait = slot - now
        if logroll and wait >= 1.0:
            logroll.logger("http").info(
                "THROTTLE %s wait=%.2fs", host, wait, extra={"file_only": True})
        time.sleep(wait)


# 熔断：同一域名（按完整 host，腾讯实时行情 qt.gtimg.cn 与日 K ifzq.gtimg.cn 分开算）连续 3 次「被拦 / 服务端错误 / 连不上」
# 就熔断 10 分钟，期间直接抛 DataSourceError，不再发请求（被封时不白等、也不继续往封禁上撞）；
# 到期后放一个请求试探，成功即恢复，失败立即再熔断。4xx 里只有 403/429 算（其余多为参数问题，不代表站点不可用）。
_TRIP_AFTER = 3
_COOLDOWN = 600
_fails, _open_until = {}, {}
_brk_lock = threading.Lock()


def _host(url):
    return urllib.parse.urlparse(url).netloc.split(":")[0]


def _counts_as_outage(e):
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500 or e.code in (403, 429)
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError, OSError))


def _breaker_check(url):
    h = _host(url)
    with _brk_lock:
        until = _open_until.get(h, 0)
    if until > time.time():
        raise DataSourceError(f"熔断中（{h} 连续失败，{int(until - time.time())} 秒后重试）: {url[:100]}")


def _breaker_record(url, ok):
    h = _host(url)
    with _brk_lock:
        if ok:
            _fails.pop(h, None)
            _open_until.pop(h, None)
            return
        n = _fails[h] = _fails.get(h, 0) + 1
        if n >= _TRIP_AFTER:
            _open_until[h] = time.time() + _COOLDOWN
            if logroll:
                logroll.logger("http").warning(
                    "BREAKER %s 触发熔断 %ds（连续失败 %d 次）", h, _COOLDOWN, n,
                    extra={"file_only": True})


def breaker_status():
    """当前熔断中的域名 → 剩余秒数（供日志 / 报错提示）。"""
    now = time.time()
    with _brk_lock:
        return {h: int(t - now) for h, t in _open_until.items() if t > now}


def http_get(url, params=None, data=None, headers=None, encoding="utf-8", timeout=10, retries=1):
    """GET（或 data 非空时 POST 表单），返回文本。失败重试 retries 次后抛 DataSourceError。"""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    hdr = {"User-Agent": UA}
    hdr.update(headers or {})
    last = None
    _breaker_check(url)
    for i in range(retries + 1):
        try:
            if i:
                _breaker_check(url)   # 重试前别的线程可能已触发熔断
            _throttle(url)
            req = urllib.request.Request(url, data=body, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, enc = r.read(), (r.headers.get("Content-Encoding") or "").lower()
            # 部分接口（如东财 F10）偶尔不管请求头直接返回压缩内容
            if raw[:2] == b"\x1f\x8b" or enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            _breaker_record(url, True)
            return raw.decode(encoding, "ignore")
        except DataSourceError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if _counts_as_outage(e):
                _breaker_record(url, False)
            if i < retries:
                time.sleep(random.uniform(1.0, 2.5) * (i + 1))   # 随机退避，越往后等越久
    raise DataSourceError(f"请求失败 {url[:120]}: {last}")


def http_json(url, **kw):
    text = http_get(url, **kw).strip()
    m = re.match(r"^[\w$.]+\((.*)\)\s*;?$", text, re.S)  # 去掉 JSONP 包装
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except ValueError as e:
        raise DataSourceError(f"非 JSON 响应 {url[:120]}: {text[:120]}") from e


def to_float(s):
    try:
        if s in ("", "-", None):
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def normalize(code):
    """把 600353 / 159813 / 0100.HK / 00100 / hk00100 / sh600353 统一成标准描述。

    返回 dict: code(纯代码), market(sh/sz/hk), tencent(腾讯行情代码), secid(东方财富 secid)
    """
    c = str(code).strip()
    low = c.lower()
    m = re.fullmatch(r"(sh|sz|hk)(\d+)", low)
    if m:
        mkt, num = m.groups()
    elif re.fullmatch(r"\d{4,5}\.hk", low):
        mkt, num = "hk", low.split(".")[0]
    elif re.fullmatch(r"\d{6}", c):
        mkt, num = ("sh" if c[0] in "569" else "sz"), c
    elif re.fullmatch(r"\d{4,5}", c):
        mkt, num = "hk", c
    else:
        raise DataSourceError(f"无法识别的代码: {code}")
    if mkt == "hk":
        num = num.zfill(5)
        secid = f"116.{num}"
    else:
        secid = ("1." if mkt == "sh" else "0.") + num
    return {"code": num, "market": mkt, "tencent": mkt + num, "secid": secid}
