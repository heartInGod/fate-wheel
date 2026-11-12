#!/usr/bin/env python3
"""可用数量推导的单元测试：python3 test_ops.py"""
import datetime
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datasources import ops

D = datetime.date
FAILED = []


def check(name, got, want):
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {name}")


def run(rows, ops_rows, days, today):
    """ops_rows: [(date_str, side, qty)]"""
    o = [ops.load_operations.__wrapped__()] if False else []
    for date_s, side, qty in ops_rows:
        o.append({"code": "600353", "date": D(*date_s), "side": side, "qty": qty})
    res = ops.derive_available(rows, o, days, today=today)
    return {r["code"]: r for r in res}


print("== 1. 跨一个交易日：基准日买入的份额次日解冻 ==")
days = [D(2026, 10, 29), D(2026, 10, 30)]
rows = [{"code": "600353", "market": "sh", "type": "A股", "shares": 3000, "available": 1500,
         "base_date": D(2026, 10, 29)}]
r = run(rows, [], days, D(2026, 10, 30))["600353"]
check("无操作记录时，基准日的冻结份额解冻", r["available"], 3000)

print("\n== 2. 基准日买入、次日仍冻结（需次日再解冻）==")
rows = [{"code": "600353", "market": "sh", "type": "A股", "shares": 3000, "available": 1500,
         "base_date": D(2026, 10, 29)}]
r = run(rows, [((2026, 10, 29), "buy", 1500)], days, D(2026, 10, 30))["600353"]
check("基准日买入的 1500 在次日解冻", r["available"], 3000)

print("\n== 3. 当日买入当日冻结，次日才解冻 ==")
days3 = [D(2026, 10, 29), D(2026, 10, 30), D(2026, 11, 2)]
rows = [{"code": "600353", "market": "sh", "type": "A股", "shares": 3000, "available": 1500,
         "base_date": D(2026, 10, 29)}]
r = run(rows, [((2026, 10, 30), "buy", 500)], days3, D(2026, 10, 30))["600353"]
check("当日买入冻结：总持仓3500-冻结500=可用3000", r["available"], 3000)
check("locked_today 记录当日冻结量", r["locked_today"], 500)
r = run(rows, [((2026, 10, 30), "buy", 500)], days3, D(2026, 11, 2))["600353"]
check("跨到 11-02 后该 500 解冻，可用3500", r["available"], 3500)

print("\n== 4. 卖出立即减少可用 ==")
r = run(rows, [((2026, 10, 30), "sell", 1000)], days3, D(2026, 10, 30))["600353"]
check("当日卖出 1000 后可用减少", r["available"], 2000)

print("\n== 5. 跨周末/节假日按交易日推进（10-30 周五 → 11-02 周一）==")
r = run(rows, [((2026, 10, 29), "buy", 1500)], days3, D(2026, 11, 2))["600353"]
check("周末不解冻两次，到 11-02 解冻一次", r["available"], 3000)

print("\n== 6. T+0 港股通：当日买入不冻结 ==")
rows_hk = [{"code": "00100", "market": "hk", "type": "港股通", "shares": 60, "available": 60,
            "base_date": D(2026, 10, 29)}]
o = [{"code": "00100", "date": D(2026, 10, 30), "side": "buy", "qty": 20}]
res = ops.derive_available(rows_hk, o, days, today=D(2026, 10, 30))
check("港股通当日买入后 locked_today=0", res[0]["locked_today"], 0)
check("港股通当日买入后 available=60+20", res[0]["available"], 80)

print("\n== 7. T+0 QDII ETF（159659）不冻结 ==")
rows_qdii = [{"code": "159659", "market": "sz", "type": "QDII ETF（T+0）", "shares": 15000, "available": 15000,
              "base_date": D(2026, 10, 29)}]
o = [{"code": "159659", "date": D(2026, 10, 30), "side": "buy", "qty": 1000}]
res = ops.derive_available(rows_qdii, o, days, today=D(2026, 10, 30))
check("QDII 当日买入不冻结", res[0]["locked_today"], 0)
check("QDII available=16000", res[0]["available"], 16000)

print("\n== 8. 无基准日 → 沿用 CSV 原值 ==")
rows_nobase = [{"code": "600353", "market": "sh", "type": "A股", "shares": 3000, "available": 1500,
                "base_date": None}]
res = ops.derive_available(rows_nobase, [], days, today=D(2026, 10, 30))
check("无基准日时可用量不变", res[0]["available"], 1500)
check("标记 available_derived=False", res[0]["available_derived"], False)

print("\n== 9. operations.csv 解析 ==")
with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig") as f:
    f.write("日期,时间,操作,名称,代码,数量,价格,备注\n")
    f.write("2026-10-30,10:05,买入,旭光电子,600353,500,19.80,加仓\n")
    f.write("2026-10-30,10:06,卖出,太极实业,sh600667,200,13.50,做T\n")
    f.write("2026-10-30,10:07,卖出,旭光电子,600353,100,19.90,减仓\n")
    f.write("2026-10-30,10:08,买入,MINIMAX,00100,10,160.0,港股\n")
    tmp = f.name
parsed = ops.load_operations(tmp)
os.unlink(tmp)
by_code = {}
for o in parsed:
    by_code.setdefault(o["code"], []).append(o)
check("解析出 4 条操作", len(parsed), 4)
check("600353 买入 500", by_code["600353"][0]["qty"], 500)
check("600353 卖出识别为 sell", by_code["600353"][1]["side"], "sell")
check("sh600667 规范为 600667", "600667" in by_code, True)
check("港股 00100 规范为 00100", "00100" in by_code, True)

print("\n== 10. 同一标的买入+卖出混合 ==")
days4 = [D(2026, 10, 29), D(2026, 10, 30)]
rows = [{"code": "600353", "market": "sh", "type": "A股", "shares": 3000, "available": 1500,
         "base_date": D(2026, 10, 29)}]
r = run(rows, [((2026, 10, 29), "buy", 1500), ((2026, 10, 30), "buy", 200),
               ((2026, 10, 30), "sell", 300)], days4, D(2026, 10, 30))["600353"]
# 基准日 available=1500；10-30 解冻 1500 → 3000；当日买入 200 冻结 → 2800；卖出 300 → 2500
check("买入卖出混合后可用：3000-300=2700", r["available"], 2700)

print("\n== 11. 基准日就是今天 → 不推进 ==")
r = run(rows, [], days, D(2026, 10, 29))["600353"]
check("基准日当天可用量不变", r["available"], 1500)

print()
if FAILED:
    print(f"FAILED {len(FAILED)} 项")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("全部通过")
