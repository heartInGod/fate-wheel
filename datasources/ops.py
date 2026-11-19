"""可用数量推导：把 holdings.csv 的静态「可用数量」换算成今天真正可卖的数量。

背景
----
A股/境内 ETF 是 T+1：当日买入的份额次日才能卖。券商 App 里的「可用数量」是
随交易日自动变化的，但 holdings.csv 里的「可用数量」是**某一天的手工快照**，
一旦跨过交易日就会失真——昨天买入被冻结的部分今天已经解冻，CSV 里却还是
昨天的数字，导致建议里给出的可卖数量被低估。

本模块的解法
------------
把 holdings.csv 的「可用数量」当作**基准日快照**，并用「可用基准日」列标明
它对应哪一天；再按交易日历把基准日到今天之间**已跨过的交易日**逐日推进：

    available(today) = available(基准日) + 该期间买入并已解冻的份额
                        - 该期间卖出（卖出立即减少可用）
                        - 当日买入（当日再次冻结）

operations.csv 记录基准日之后的每一笔成交（买入/卖出），是推进过程的唯一依据。
基准日当天及之前若没有记录，则认为该期间无成交。

规则细节
--------
* 交易日历取上证指数日 K 的日期序列，天然跳过周末与休市日。
* 基准日之后的买入，在**下一个交易日**才解冻；卖出当日即减少可用。
* 当日买入当日冻结，不参与解冻。
* 港股通标的是 T+0（当日可卖），不适用冻结；QDII ETF（159659）标的是 T+0。
* 无法确定基准日时（缺列或日期非法），退化为直接使用 CSV 里的可用数量。
"""
import csv
import datetime
import os
import re

from paths import ROOT
from datamul import data_path
OPERATIONS_CSV = data_path("operations.csv")

# T+0（当日买入即可卖）的市场类型：港股通、QDII ETF
T0_MARKETS = ("港股通", "QDII")
# 已知 T+0 的跨境 ETF 代码
T0_CODES = {"159659"}

_BUY_WORDS = ("买入", "买", "申购", "增持")
_SELL_WORDS = ("卖出", "卖", "赎回", "减持")


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _norm_code(raw):
    """把 operations.csv 里的代码规范成 holdings.csv 的写法（去掉 sh/sz/hk 前缀、保留 5/6 位）。"""
    c = str(raw or "").strip().upper()
    if not c:
        return ""
    m = re.fullmatch(r"(SH|SZ|HK)(\d+)", c)
    if m:
        c = m.group(2)
    c = c.replace(".HK", "")
    # 港股统一补成 5 位，与 holdings.csv 的 0100 一致
    if re.fullmatch(r"\d{1,5}", c):
        n = c.lstrip("0") or "0"
        if len(n) <= 5 and (len(c) == 5 or c.startswith("0")) and len(n) <= 4:
            return c.zfill(5)
    return c


def _parse_date(s):
    s = str(s or "").strip().replace("/", "-")
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y.%m.%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _is_buy(op):
    op = str(op or "").strip()
    if any(w in op for w in _SELL_WORDS):
        return False
    return any(w in op for w in _BUY_WORDS)


def _to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def load_operations(path=OPERATIONS_CSV):
    """读 operations.csv，归一化成 {code, date, side, qty, op, price, name, raw}。"""
    out = []
    for r in _read_csv(path):
        code = _norm_code(r.get("代码"))
        d = _parse_date(r.get("日期"))
        qty = _to_float(r.get("数量"))
        if not code or not d or qty <= 0:
            continue
        out.append({"code": code, "date": d, "side": "buy" if _is_buy(r.get("操作")) else "sell",
                    "qty": qty, "op": (r.get("操作") or "").strip(),
                    "price": _to_float(r.get("价格")), "name": (r.get("名称") or "").strip(),
                    "raw": r})
    return out


def trading_days(codes=("sh000001",), count=40):
    """交易日历：取上证指数日 K 的日期序列（含当天）。失败时返回空列表。"""
    from . import quotes
    import datetime as _dt
    now = _dt.datetime.now()
    today = now.strftime("%Y-%m-%d")
    days = []
    for c in codes:
        rows, at = quotes.cached_kline(c)
        # 今天开盘后（09:30 起）抓过的缓存已含当天这根，直接用，不联网；开盘前抓的缓存只在开盘前用
        fresh_cache = bool(rows) and bool(at) and at[:10] == today and (at[11:16] >= "09:30" or now.strftime("%H:%M") < "09:30")
        if not fresh_cache:
            try:
                rows = quotes.kline(c, count=count)
            except Exception:  # noqa: BLE001  联网失败（含熔断）退回缓存
                pass
        for row in (rows or [])[-count:]:
            d = _parse_date(row.get("date"))
            if d:
                days.append(d)
    return sorted(set(days))


def _is_t0(row):
    """判断是否 T+0（当日买入当日可卖）。

    holdings.csv 的「市场」列才是中文市场名（港股通 / QDII ETF（T+0）/ A股 / A股ETF），
    normalize 后的 market 字段只有 sh/sz/hk，不能用来判断。
    """
    typ = str(row.get("type") or "")
    if any(t in typ for t in T0_MARKETS):
        return True
    # 兜底：按代码判断，159659 是跨境 QDII ETF
    return str(row.get("code") or "") in T0_CODES


def derive_available(rows, ops, days, today=None):
    """把 holdings.csv 的静态可用数量推进到今天。

    rows: [{code, market, shares, available, base_date}] —— base_date 为基准日（date 或 None）
    ops:  load_operations() 的结果
    days: 交易日历（升序 date 列表）
    返回同结构列表，键 available / locked_today / base_available / base_date 被补齐。
    """
    today = today or datetime.date.today()
    ops_by_code = {}
    for o in ops:
        ops_by_code.setdefault(o["code"], []).append(o)

    # 交易日历：只保留 <= today 的日子（days_sorted 有序，day_set 用于成员判断）
    days_sorted = sorted(d for d in days if d <= today)
    day_set = set(days_sorted)

    out = []
    for r in rows:
        code = r["code"]
        base_date = r.get("base_date")
        base_avail = r.get("available")
        shares = r.get("shares") or 0
        t0 = _is_t0(r)

        if not base_date or base_date not in day_set or not day_set:
            # 没有基准日（或基准日不是交易日）→ 无法推进，沿用 CSV 原值
            out.append({**r, "locked_today": 0, "base_available": base_avail,
                        "base_date": base_date, "available_derived": False})
            continue

        code_ops = ops_by_code.get(code, [])
        # 只取基准日之后的操作（基准日当天的成交已包含在 CSV 快照里）
        rel = [o for o in code_ops if o["date"] > base_date]

        # 双轨跟踪：shares（总持仓）与 locked（T+1 冻结份额）。
        #   基准日收盘时：shares = CSV 持仓数量，locked = shares - base_avail
        #   每个交易日开盘：上一交易日的冻结份额解冻（locked 归零后只留当日买入）
        #   当日买入 → shares 增加、locked 增加；当日卖出 → shares 减少
        #   available = shares - locked
        shares_now = shares
        locked = max(0.0, (shares or 0) - base_avail) if not t0 else 0.0
        avail = base_avail
        locked_today = 0

        for d in days_sorted:
            if d <= base_date:
                continue
            # 开盘：上一交易日的冻结份额解冻
            prev = _prev_trading_day(days_sorted, d)
            if prev is not None:
                if prev == base_date:
                    unlocked = max(0.0, (shares or 0) - base_avail) if not t0 else 0.0
                else:
                    unlocked = sum(o["qty"] for o in rel
                                   if o["date"] == prev and o["side"] == "buy")
                locked = max(0.0, locked - unlocked)
                avail += unlocked
            # 当日成交
            bought = sum(o["qty"] for o in rel if o["date"] == d and o["side"] == "buy")
            sold = sum(o["qty"] for o in rel if o["date"] == d and o["side"] == "sell")
            if bought:
                shares_now += bought
                if t0:
                    avail += bought          # T+0 当日买入即可卖
                else:
                    locked += bought
                    locked_today += bought
            if sold:
                shares_now -= sold
                avail -= sold

        avail = max(0.0, min(avail, shares_now if shares_now else avail))
        out.append({**r, "available": avail, "locked_today": locked_today,
                    "base_available": base_avail, "base_date": base_date,
                    "available_derived": True})
    return out


def _prev_trading_day(days_sorted, d):
    """days_sorted 必须是有序列表；返回 d 之前最近的一个交易日，没有则返回 None。"""
    prev = None
    for x in days_sorted:
        if x < d:
            prev = x
        else:
            break
    return prev


def _next_trading_day(day_set, d):
    for x in day_set:
        if x > d:
            return x
    return None
