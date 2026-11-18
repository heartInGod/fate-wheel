#!/usr/bin/env python3
import os as _os, sys as _sys
_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _root)
_sys.path.insert(0, _os.path.join(_root, "common"))
from paths import setup_sys_path as _ssp; _ssp()
"""规则引擎 / 信号记录 / 收盘评估的离线单元测试：python3 test_signals.py

不联网、不碰真实数据：信号目录和快照目录都指向临时目录，行情用假数据替换。
"""
import contextlib
import io
import json
import os
import sys
import tempfile

from paths import ROOT, setup_sys_path; setup_sys_path()
from datamul import data_path

import evaluate as ev
import record_signal as rs
import signal_store as st
import signals_engine as se

FAILED = []
TMP = tempfile.mkdtemp(prefix="fw_test_")
st.SIG_DIR = os.path.join(TMP, "signals")
se.SNAP_DIR = os.path.join(TMP, "snapshots")
PARAMS = st.load_params()
import core_value as cv
cv.load_core = lambda params, today=None: None   # 不读真实 core_auto.json：默认全部按交易仓，两仓用例单独构造
PARAMS["auto"]["enabled"] = False   # 1~5 节测固定阈值；动态阈值/EV 在第 6 节用构造的 auto 测
DAY = "20261030"


def check(name, got, want):
    if got != want:
        FAILED.append(name)
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {name}")


def quiet(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **k)
    return r, buf.getvalue()


def pos(**kw):
    base = {"code": "600000", "name": "测试股", "market": "sh", "price": 10.0, "low": 9.8, "high": 10.2,
            "prev_close": 10.0, "pct": 0.0, "available": 1000, "shares": 1000, "stance": "", "tech": {}}
    base.update(kw)
    return base


def snap(positions, now="2026-10-30 10:20:20", cash=100000):
    return {"market": {"now": now, "a_open_today": True}, "account": {"cash_available": cash},
            "positions": positions}


def types(cands):
    return sorted(c["type"] for c in cands)


# ------------------------------------------------------------------ 规则引擎
print("== 1. 规则引擎：各类候选 ==")
c = se.evaluate_position(pos(code="00100", market="hk", price=154.0, low=153.0, high=160.0,
                             prev_close=158.0, pct=-2.5, available=60, shares=60), PARAMS, 0)
check("跌破 stop_levels 155 → 止损", types(c), ["stop"])
check("止损数量不超过可用（港股每手 10）", c[0]["qty"], 60)

c = se.evaluate_position(pos(code="00100", market="hk", price=162.0, low=158.0, high=166.0,
                             prev_close=166.0, pct=-2.4, available=60, shares=60), PARAMS, 0)
check("跌破警戒线 165 但未破 155 → 异动", types(c), ["alert"])

c = se.evaluate_position(pos(stance="逢反弹减仓", price=10.0, low=9.8, high=10.3, available=700,
                             tech={"ma5": 10.05}), PARAMS, 0)
r = [x for x in c if x["type"] == "reduce"]
check("回升 2% 且距 MA5 0.5% → 反弹减仓", len(r), 1)
check("减仓数量 = 可用 700 × 0.5 取整手 = 300", r[0]["qty"] if r else None, 300)

c = se.evaluate_position(pos(stance="逢反弹减仓", price=10.0, low=9.8, high=10.3, available=700,
                             prev_close=10.6, pct=-5.7, tech={"ma5": 10.5}), PARAMS, 0)
check("压力位太远（5%）→ 不出减仓", [x for x in c if x["type"] == "reduce"], [])

c = se.evaluate_position(pos(stance="做T", price=10.28, low=9.9, high=10.3, prev_close=10.0,
                             available=500, shares=1500), PARAMS, 0)
th = [x for x in c if x["type"] == "t_high"]
check("振幅 4% 且处于区间顶部 → 做T高抛", len(th), 1)
check("高抛数量 ≤ 可用", (th[0]["qty"] <= 500) if th else None, True)
# 振幅 4% → d = max(1.5, 4×0.5, 交易仓目标价差 3)=3%，回补区 = [10.28×(1-3.5%), 10.28×(1-3%)]
check("高抛成对：两腿价差 ≥ 3%（回补区低于高抛价 3%~3.5%）", th[0].get("pair_zone") if th else None, [9.92, 9.97])
c = se.evaluate_position(pos(stance="做T", price=10.06, low=9.9, high=10.3, prev_close=10.0), PARAMS, 0)
tl = [x for x in c if x["type"] == "t_low"]
check("低吸成对：卖回区高于低吸价 3%~3.5%", tl[0].get("pair_zone") if tl else None, [10.36, 10.41])

c = se.evaluate_position(pos(stance="做T", price=10.12, low=9.9, high=10.3, prev_close=10.0), PARAMS, 0)
check("振幅够但在区间中部 → 不出做T", [x for x in c if x["type"] in ("t_high", "t_low")], [])

c = se.evaluate_position(pos(stance="做T", price=10.25, low=10.0, high=10.28, prev_close=10.2), PARAMS, 0)
check("振幅 < 3% → 不出做T", [x for x in c if x["type"] in ("t_high", "t_low")], [])

c = se.evaluate_position(pos(code="159659", stance="分批加仓", is_etf=True, premium_pct=4.0, price=1.02,
                             low=1.0, high=1.05, pct=-3.5, tech={"low20": 1.0}), PARAMS, 100000)
a = [x for x in c if x["type"] == "add"]
check("溢价 4% > 上限 3% → 加仓被屏蔽", a[0]["status"] if a else None, "blocked")

c = se.evaluate_position(pos(stance="分批加仓", price=10.2, low=10.0, high=10.6, pct=-3.2,
                             tech={"low20": 10.1}), PARAMS, 100000)
a = [x for x in c if x["type"] == "add"]
check("跌 3.2% 后回升 2% → 加仓候选", a[0]["status"] if a else None, "new")
check("加仓金额 = min(10 万×3%, 1 万) → 200 股", a[0]["qty"] if a else None, 200)

c = se.evaluate_position(pos(price=9.3, low=9.2, high=10.0, pct=-7.0), PARAMS, 0)
check("日内跌 7% → 异动", types(c), ["alert"])

c = se.evaluate_position(pos(price=10.0, is_today=False), PARAMS, 0)
check("非当日行情 → 不出信号", c, [])

print("\n== 2. 引擎 run：二选一、记录、去重 ==")
s = snap([
    pos(code="159327", name="半导体A", stance="分批加仓", price=1.02, low=1.0, high=1.05, pct=-3.5),
    pos(code="159813", name="半导体B", stance="分批加仓", price=1.016, low=1.0, high=1.05, pct=-3.5),
    pos(code="600001", name="减仓股", stance="逢反弹减仓", price=10.0, low=9.8, high=10.3, tech={"ma5": 10.05}),
])
res = se.run(s, PARAMS, record=True)
check("时点识别 10:20:20 → 10:20", res["slot"], "10:20")
check("二选一：只留回升更多的 159327", [x["code"] for x in res["candidates"] if x["type"] == "add"], ["159327"])
check("落选的 159813 进 suppressed", [x["status"] for x in res["suppressed"]], ["group_suppressed"])
sigs = st.load_signals(DAY)
check("候选与落选都写入 jsonl", len(sigs), 3)

s2 = snap([pos(code="600001", name="减仓股", stance="逢反弹减仓", price=10.1, low=9.8, high=10.3,
               tech={"ma5": 10.15})], now="2026-10-30 10:30:10")
res2 = se.run(s2, PARAMS, record=True)
check("价格变化 1% < 2% → 同向去重", [d["code"] for d in res2["duplicates"]], ["600001"])

s3 = snap([pos(code="600001", name="减仓股", stance="逢反弹减仓", price=10.4, low=10.1, high=10.6,
               tech={"ma5": 10.45})], now="2026-10-30 10:45:10")
res3 = se.run(s3, PARAMS, record=True)
check("价格变化 4% → 允许再次提醒", [x["code"] for x in res3["candidates"]], ["600001"])

rid = [x["id"] for x in res["candidates"] if x["code"] == "600001"][0]
quiet(rs.cmd_mark, DAY, [rid], "send_failed")
s4 = snap([pos(code="600001", name="减仓股", stance="逢反弹减仓", price=10.0, low=9.8, high=10.3,
               tech={"ma5": 10.05})], now="2026-10-30 11:00:10")
res4 = se.run(s4, PARAMS, record=False)
check("发送失败的信号不参与去重 → 可重提", [x["code"] for x in res4["candidates"]], ["600001"])

print("\n== 3. record_signal ==")
r, out = quiet(rs.cmd_decide, DAY, [res3["candidates"][0]["id"], "reject", "--note", "量能不配合"])
check("decide reject 成功", r, 0)
check("否决后状态", st.load_signals(DAY)[res3["candidates"][0]["id"]].get("decision"), "reject")
r, out = quiet(rs.cmd_add, DAY, ["--code", "600001", "--type", "减仓", "--price", "10.45", "--time", "10:50"])
check("被否决的同向信号仍参与去重 → add 拒绝（返回 3）", r, 3)
r, out = quiet(rs.cmd_add, DAY, ["--code", "600001", "--type", "减仓", "--price", "10.45", "--time", "10:50", "--force"])
check("--force 可强制记录", r, 0)
r, out = quiet(rs.cmd_add, DAY, ["--code", "600002", "--type", "做T低吸", "--zone", "9.8~10.0", "--time", "08:00"])
check("做T低吸不带 --back → 拒绝（返回 2）", r, 2)
r, out = quiet(rs.cmd_add, DAY, ["--code", "600002", "--type", "做T低吸", "--zone", "9.8~10.0",
                                 "--back", "9.9~10.1", "--time", "08:00"])
check("卖回区不高于低吸价 → 拒绝（返回 2）", r, 2)
r, out = quiet(rs.cmd_add, DAY, ["--code", "600003", "--type", "做T高抛", "--zone", "10.2~10.3",
                                 "--back", "10.25~10.4", "--time", "08:00"])
check("回补区不低于高抛价 → 拒绝（返回 2）", r, 2)
r, out = quiet(rs.cmd_add, DAY, ["--code", "600002", "--type", "做T低吸", "--zone", "9.8~10.0",
                                 "--back", "10.2~10.3", "--time", "08:00"])
check("做T低吸成对 → 记录成功", r, 0)
sid = out.strip().splitlines()[-1]
check("卖回区写入", st.load_signals(DAY)[sid].get("pair_zone"), [10.2, 10.3])
check("盘前 add 自动标 llm_premarket", st.load_signals(DAY)[sid]["rule"], "llm_premarket")
check("区间解析", st.load_signals(DAY)[sid]["zone"], [9.8, 10.0])
r, out = quiet(rs.cmd_add, DAY, ["--code", "600005", "--type", "加仓", "--price", "10", "--time", "10:00"])
check("加仓 add 成功", r, 0)
sid5 = out.strip().splitlines()[-1]
check("买入类自动带 +3% 止盈价/持有天数", (st.load_signals(DAY)[sid5].get("tp_price"), st.load_signals(DAY)[sid5].get("hold_days")), (10.3, 5))
r, out = quiet(rs.cmd_add, DAY, ["--code", "600006", "--type", "加仓", "--price", "10", "--tp", "10.8", "--time", "10:00"])
check("--tp 可改写止盈价", st.load_signals(DAY)[out.strip().splitlines()[-1]].get("tp_price"), 10.8)
check("数量解析：1万~1.5万股", rs._qty("建议卖 1万~1.5万股"), 10000)
st.append_events(DAY, [{"event": "candidate", "id": "T-OLD", "code": "600004", "type": "t_high",
                        "time": "10:00", "price": 10.0, "zone": [10.0, 10.1], "status": "new"}])
r, out = quiet(rs.cmd_decide, DAY, ["T-OLD", "accept"])
check("采纳没有回补区的做T候选 → 拒绝（返回 2）", r, 2)
r, out = quiet(rs.cmd_decide, DAY, ["T-OLD", "accept", "--back", "9.8~9.85"])
check("采纳时用 --back 补上回补区 → 成功", (r, st.load_signals(DAY)["T-OLD"].get("pair_zone")), (0, [9.8, 9.85]))
r, out = quiet(rs.cmd_decide, DAY, ["T-OLD", "reject"])
check("否决做T不要求回补区", r, 0)

# ------------------------------------------------------------------ 评估
print("\n== 4. evaluate：评分口径 ==")
BARS = {}
DAILY = {}
ev.intraday_bars = lambda code, day: (BARS.get(code, []), False)
ev.daily = lambda code, count=30: DAILY.get(code, [])
BARS["sh000300"] = [("0930", 4000, 4000, 4000), ("1500", 4000, 4000, 4000)]


def bars(*pts):
    return [(t, p, p, p) for t, p in pts]


import copy as _copy
PEV1 = _copy.deepcopy(PARAMS)
PEV1["eval"]["t_target_pct"] = 1.0   # 下面做T用例按 1% 回转目标写；3% 的单独测


def e(sig, tdays=(DAY,), params=PEV1):
    sig.setdefault("time", "10:00")
    return ev.evaluate_signal(sig, DAY, list(tdays), params)


BARS["600011"] = bars(("1000", 10.0), ("1030", 9.9), ("1100", 10.05), ("1500", 9.8))
r = e({"code": "600011", "type": "reduce", "price": 10.0, "zone": [10.0, 10.1]})
check("减仓后收盘跌 2% → edge +2%、✅", (r["edge_close"], r["verdict"]), (2.0, "✅"))
r = e({"code": "600011", "type": "add", "price": 10.0})
check("加仓后收盘跌 2% → ❌", (r["edge_close"], r["verdict"]), (-2.0, "❌"))
r = e({"code": "600011", "type": "alert", "price": 10.0})
check("异动不评方向", r["verdict"], "—")

BARS["600012"] = bars(("1000", 10.0), ("1030", 9.8), ("1100", 9.95), ("1500", 9.7))
r = e({"code": "600012", "type": "t_low", "price": 10.0, "zone": [9.75, 9.8]})
check("低吸：到 9.8 触发、最高回到 9.95（+1.5%）→ ✅", (r["t_entry"], r["verdict"]), (9.8, "✅"))
r = e({"code": "600012", "type": "t_low", "price": 10.0, "zone": [9.75, 9.8]}, params=PARAMS)
check("低吸：当前口径 3%，只回到 +1.5% → 未成功（⏳ 待次日）", r["verdict"], "⏳")
# 交易仓买入带止盈：5 个交易日内最高价到 +3% 记 +3%，否则按第 5 日收盘
BARS["600015"] = bars(("1000", 10.0), ("1500", 10.0))
_td = (DAY, "20261102", "20261103", "20261104", "20261105", "20261106")
DAILY["600015"] = [("20261102", 10, 10.1, 10.2, 9.9), ("20261103", 10, 10.0, 10.31, 9.9)]
r = e({"code": "600015", "type": "watch_dip", "price": 10.0, "tp_price": 10.3, "hold_days": 5}, tdays=_td)
check("止盈：第 2 日最高 10.31 ≥ 10.3 → tp_edge +3%", (r.get("tp_edge"), r.get("tp_hit")), (3.0, "20261103"))
DAILY["600015"] = [(d, 10, 9.8, 10.1, 9.7) for d in _td[1:]]
r = e({"code": "600015", "type": "add", "price": 10.0, "tp_price": 10.3, "hold_days": 5}, tdays=_td)
check("止盈：5 日没到价 → 按第 5 日收盘 −2%", r.get("tp_edge"), -2.0)
DAILY["600015"] = DAILY["600015"][:2]
r = e({"code": "600015", "type": "add", "price": 10.0, "tp_price": 10.3, "hold_days": 5}, tdays=_td[:3])
check("止盈：不满 5 日且没到价 → 不出 tp_edge", r.get("tp_edge"), None)
BARS["600013"] = bars(("1000", 10.0), ("1030", 9.8), ("1100", 9.85), ("1500", 9.7))
r = e({"code": "600013", "type": "t_low", "price": 10.0, "zone": [9.75, 9.8]})
check("低吸：触发但只反弹 0.5%、无次日 → ⏳", r["verdict"], "⏳")
DAILY["600013"] = [("20261102", 9.7, 9.9, 10.0, 9.6)]
r = e({"code": "600013", "type": "t_low", "price": 10.0, "zone": [9.75, 9.8]}, tdays=(DAY, "20261102"))
check("低吸：次日最高 10.0 ≥ 9.8×1.01 → ✅ 次日", (r["verdict"], r["t_success_when"]), ("✅", "次日"))
BARS["600014"] = bars(("1000", 10.0), ("1100", 10.1), ("1500", 10.05))
r = e({"code": "600014", "type": "t_low", "price": 10.0, "zone": [9.7, 9.8]})
check("低吸：当天没到价 → ⚪ 未触发、不算收益", (r["verdict"], r.get("edge_close")), ("⚪", None))
r = e({"code": "600014", "type": "t_high", "price": 10.0, "zone": [10.1, 10.2]})
check("高抛：到 10.1 触发、收 10.05 回落 0.5% → ⏳", (r["t_entry"], r["verdict"]), (10.1, "⏳"))
BARS["600015"] = bars(("1000", 10.0), ("1030", 10.2), ("1100", 9.95), ("1500", 10.1))
r = e({"code": "600015", "type": "t_high", "price": 10.0, "zone": [10.2, 10.25], "pair_zone": [9.9, 9.95]})
check("成对高抛：10.2 卖出、9.95 回补成交 → ✅、两腿价差 +2.45%",
      (r["verdict"], r["verdict_basis"], r.get("t_pair_edge")), ("✅", "当日回补成交", 2.45))
r = e({"code": "600015", "type": "t_high", "price": 10.0, "zone": [10.2, 10.25], "pair_zone": [9.8, 9.9]},
      tdays=(DAY, "20261102"))
DAILY["600015"] = [("20261102", 10.1, 10.3, 10.4, 10.0)]
r = e({"code": "600015", "type": "t_high", "price": 10.0, "zone": [10.2, 10.25], "pair_zone": [9.8, 9.9]},
      tdays=(DAY, "20261102"))
check("成对高抛：回补区当日和次日都没到 → ❌ 回补未成交", (r["verdict"], r["verdict_basis"]), ("❌", "回补未成交"))
r = e({"code": "600011", "type": "reduce", "price": 9.8, "time": "14:55"})
check("尾盘信号不按收盘评 → ⏳", r["verdict"], "⏳")
DAILY["600011"] = [("20261102", 9.8, 9.5, 9.9, 9.4)]
r = e({"code": "600011", "type": "reduce", "price": 9.8, "time": "14:55"}, tdays=(DAY, "20261102"))
check("尾盘信号有 T+1 后按 T+1 评（跌 3.1%）→ ✅", (r["verdict"], r["verdict_basis"]), ("✅", "T+1"))
r = e({"code": "600011", "type": "reduce", "price": None, "time": "08:00"})
check("盘前无价 → 取开盘后第一笔价格", (r["p0"], r.get("price_filled")), (10.0, True))

print("\n== 4b. 自选股（只找买点） ==")


def wrow(**kw):
    base = {"code": "600100", "name": "自选A", "price": 10.15, "low": 9.95, "high": 10.2, "prev_close": 10.2,
            "pct": -0.5, "volume_ratio": 1.0, "is_today": True, "limit_up": 11.22, "limit_down": 9.18,
            "tech": {"ma20": 10.0, "ma60": 11.0, "low20": 9.5, "high20": 11.5, "low60": 9.0}}
    base.update(kw)
    return base


c = se.evaluate_watch(wrow(), PARAMS, 100000)
check("回升 2% 且贴近 MA20 → 自选低吸", types(c), ["watch_dip"])
check("自选低吸方向为买入、带 watch 标记", (c[0]["side"], c[0]["watch"], c[0]["label"]), (1, True, "👀自选低吸"))
check("首仓金额 min(10万×3%, 1万)=3000 元 / 10.15 → 200 股", c[0]["qty"], 200)
check("自选低吸：涨 4% 不算低吸", types(se.evaluate_watch(wrow(pct=4.0), PARAMS, 100000)), [])
check("自选低吸：回升不足 1.5% 不出", types(se.evaluate_watch(wrow(low=10.1), PARAMS, 100000)), [])
check("自选低吸：远离支撑不出", types(se.evaluate_watch(wrow(tech={"ma20": 9.0, "ma60": 8.0, "low20": 8.5, "high20": 12}), PARAMS, 100000)), [])
check("自选：行情非今日不出", types(se.evaluate_watch(wrow(is_today=False), PARAMS, 100000)), [])
brk = wrow(price=11.8, low=11.3, high=11.85, prev_close=11.5, pct=2.6, volume_ratio=2.0, limit_up=12.65)
check("放量站上前20日高 → 自选突破", types(se.evaluate_watch(brk, PARAMS, 100000)), ["watch_break"])
check("自选突破：量比不足不出", types(se.evaluate_watch(dict(brk, volume_ratio=1.2), PARAMS, 100000)), [])
check("自选突破：涨 8% 不追", types(se.evaluate_watch(dict(brk, pct=8.0), PARAMS, 100000)), [])
check("自选突破：贴涨停不追", types(se.evaluate_watch(dict(brk, limit_up=11.82), PARAMS, 100000)), [])
c = se.evaluate_watch(wrow(code="688001", price=101.5, low=99.5, high=102, prev_close=102,
                           tech={"ma20": 100, "ma60": 110, "low20": 95, "high20": 115}), PARAMS, 100000)
check("688 最小 200 股、超出建议金额要标记", (c[0]["qty"], bool(c[0]["flags"])), (200, True))
q, fl = se.watch_qty("600100", 10.0, 100000, PARAMS["watch"])
check("watch_qty：3000 元 / 10 元 → 300 股、不标记", (q, fl), (300, []))

ws = snap([pos(code="600000")], now="2026-10-30 10:30:10")
ws["watchlist"] = [wrow(code=f"60010{i}", name=f"自选{i}", low=9.95 - i * 0.05) for i in range(5)]
ws["watchlist"].append(wrow(code="600000", name="已持有"))   # 已持有的不按自选评
rw = se.run(ws, PARAMS, record=True)
check("每时点最多 3 条自选，其余 capped", (len([c for c in rw["candidates"] if c.get("watch")]),
                                     len([c for c in rw["suppressed"] if c["status"] == "capped"])), (3, 2))
check("capped 按回升幅度排序：留下回升最大的三只",
      sorted(c["code"] for c in rw["candidates"] if c.get("watch")), ["600102", "600103", "600104"])
check("已持有的标的不出自选信号", any(c["code"] == "600000" and c.get("watch") for c in rw["candidates"]), False)
sigs = st.load_signals(DAY)
check("capped 不落库", any(s.get("status") == "capped" for s in sigs.values()), False)
rw2 = se.run(dict(ws, market={"now": "2026-10-30 10:45:10", "a_open_today": True}), PARAMS, record=True)
check("下一时点同价 → 已出的 3 只判重复，落选的 2 只补上",
      (len(rw2["duplicates"]), sorted(c["code"] for c in rw2["candidates"] if c.get("watch"))),
      (3, ["600100", "600101"]))
check("别名：自选低吸/建仓/自选突破", [st.norm_type(x) for x in ("自选低吸", "建仓", "自选突破")],
      ["watch_dip", "watch_dip", "watch_break"])
lv = se.premarket_levels({"positions": [], "watchlist": [wrow()]}, PARAMS)
check("盘前输出自选股支撑/压力位", (len(lv["watch_levels"]), lv["watch_levels"][0]["supports"][0][0]), (1, "MA20"))
BARS["600100"] = bars(("1030", 10.15), ("1100", 10.3), ("1500", 10.35))
r = e({"code": "600100", "type": "watch_dip", "price": 10.15, "zone": [9.95, 10.15], "time": "10:30"})
check("自选低吸按买入方向评估：收盘涨 2% → ✅", r["verdict"], "✅")

print("\n== 4c. 大盘脉搏（国家队迹象，只作背景） ==")


def mpulse(hs, ret15=0.0, rebound=0.0, small=1.0):
    return {"as_of": "20261030 1015",
            "etfs": [{"code": f"51030{i}", "group": "hs300", "vol_ratio": hs, "ret_15m": ret15} for i in range(4)],
            "groups": {"hs300": {"median_ratio": hs}, "small": {"median_ratio": small}},
            "indices": [{"name": "沪深300", "pct": 0.5, "from_low": rebound}]}


check("无 market_pulse → none", se.classify_pulse({}, PARAMS)["level"], "none")
check("沪深300ETF 1.1 倍 → normal", se.classify_pulse({"market_pulse": mpulse(1.1)}, PARAMS)["level"], "normal")
check("沪深300ETF 6 倍 → strong", se.classify_pulse({"market_pulse": mpulse(6.0)}, PARAMS)["level"], "strong")
check("2.2 倍且指数从低点回升 1.2% → watch",
      se.classify_pulse({"market_pulse": mpulse(2.2, rebound=1.2)}, PARAMS)["level"], "watch")
check("2.2 倍但没回升 → normal", se.classify_pulse({"market_pulse": mpulse(2.2, rebound=0.3)}, PARAMS)["level"], "normal")
check("1.6 倍且 15 分钟 +1% → watch（急拉）",
      se.classify_pulse({"market_pulse": mpulse(1.6, ret15=1.0)}, PARAMS)["level"], "watch")
pn = se.classify_pulse({"market_pulse": mpulse(1.1), "policy_news": [{"time": "2026-10-30 10:02:00", "text": "【汇金增持ETF】"}]}, PARAMS)
check("政策快讯列入 reasons / policy_news", (pn["reasons"][-1], pn["policy_news"][0][:5]), ("政策快讯 1 条", "10:02"))

import datetime as _dt
from datasources import market_pulse as _mp
_today = _dt.date.today().strftime("%Y%m%d")
_fake = {f"2026010{i}": [("0935", 1.0, 1.0, 1.0, 100.0), ("1500", 1.0, 1.0, 1.0, 1000.0)] for i in range(1, 7)}
_fake["20260106"] = [("0935", 1.0, 1.0, 1.0, 300.0), ("1500", 1.0, 1.0, 1.0, 3000.0)]
_fake[_today] = [("2359", 1.0, 1.0, 1.0, 5.0)]          # 只有一根未走完、只含竞价量的 K
_orig_m5 = _mp.m5_bars
_mp.m5_bars = lambda code, count=320: _fake
try:
    _m = _mp.etf_metrics("510300")
finally:
    _mp.m5_bars = _orig_m5
check("9:25~9:35 只有竞价 K → 回退上一交易日全天量比", (_m["day"], _m["vol_ratio"]), ("20260106", 3.0))


def mshares(d1, hist=None):
    hist = hist if hist is not None else [d1]
    return {"date": "2026-11-02", "groups": {"hs300": {"chg_pct": d1, "chg_yi": d1 * 7}},
            "history": [{"date": f"d{i}", "groups": {"hs300": x}} for i, x in enumerate(hist)]}


check("份额 +6%（量能正常）→ strong（取较高）",
      se.classify_pulse({"market_pulse": {**mpulse(1.1), "shares": mshares(6.0)}}, PARAMS)["level"], "strong")
check("份额 +3% → watch", se.classify_pulse({"market_pulse": {"shares": mshares(3.0)}}, PARAMS)["level"], "watch")
check("份额单日 +1.5% 但 5 日累计 >8% → watch",
      se.classify_pulse({"market_pulse": {"shares": mshares(1.5, [1.5, 2.0, 2.0, 2.0, 1.5])}}, PARAMS)["level"], "watch")
ps = se.classify_pulse({"market_pulse": {**mpulse(6.0), "shares": mshares(0.3, [0.3, -0.5])}}, PARAMS)
check("份额平常 + 量能 strong → strong，reasons 含份额", (ps["level"], any("份额" in r for r in ps["reasons"])), ("strong", True))
check("只有份额且平常 → normal", se.classify_pulse({"market_pulse": {"shares": mshares(0.2)}}, PARAMS)["level"], "normal")
sp = snap([pos(code="00100", market="hk", price=154.0, low=153.0, high=160.0, prev_close=158.0, pct=-2.5,
               available=60, shares=60)], now="2026-10-30 13:15:10")
sp["market_pulse"] = mpulse(6.0)
rp = se.run(sp, PARAMS, record=False)
stop = [c for c in rp["candidates"] + rp["duplicates"] if c["type"] == "stop"]
check("strong 时止损候选带“勿追跌”flag、仍保留候选",
      (rp["market_pulse"]["level"], bool(stop), any("勿追跌" in f for c in rp["candidates"] if c["type"] == "stop" for f in c["flags"])),
      ("strong", True, True))
sp["market_pulse"] = mpulse(1.0)
rp = se.run(sp, PARAMS, record=False)
check("normal 时不加 flag", any("勿追跌" in f for c in rp["candidates"] for f in c["flags"]), False)
check("盘前也输出 market_pulse", se.premarket_levels({"positions": [], "market_pulse": mpulse(3.5)}, PARAMS)["market_pulse"]["level"], "strong")

print("\n== 5. collect_snapshot.match_slot ==")
try:
    import collect_snapshot as cs
    for hm, want in (("08:59", "09:00"), ("11:19", "11:20"), ("09:39", "09:40"), ("14:59", "15:00"),
                     ("10:16", "10:10"), ("10:13", "10:10"), ("11:45", "11:30")):
        h, m = map(int, hm.split(":"))
        check(f"{hm} → {want}", (cs.match_slot(h * 60 + m) or (None, None))[1], want)
except ImportError as e:  # noqa: F841
    print("  skip（collect_snapshot 不可导入）")

print("\n== 6. 动态阈值 / EV / 仓位 ==")
import copy as _copy
import auto_params as ap
import fees
check("ETF 1 万往返手续费 10 元", round(fees.round_trip(10000, "159813"), 2), 10.0)
check("A 股 1 万往返 15.2 元", round(fees.round_trip(10000, "600000"), 2), 15.2)
check("港股识别", (fees.kind_of("00100"), fees.kind_of("0100.HK"), fees.kind_of("159813"), fees.kind_of("600000")),
      ("hk", "hk", "etf", "stock"))
PA = _copy.deepcopy(PARAMS)
PA["auto"]["enabled"] = True
PA["auto"]["sizing"]["min_ev_yuan"] = 0   # 下面的用例先不设 EV 门槛，门槛单独测
pe = ap.effective(PA, {"t_amp_k": 1.0, "add_drop_k": 0.5, "t_high_pos": 0.7}, 4.0)
check("effective：atr 类 = k×ATR，跌幅取负，raw 直接用",
      (pe["t"]["min_amplitude_pct"], pe["add"]["drop_trigger_pct"], pe["t"]["high_pos"]), (4.0, -2.0, 0.7))
check("effective 不改原参数", PA["t"]["min_amplitude_pct"], PARAMS["t"]["min_amplitude_pct"])
check("atr 缺失时 atr 类保持固定值", ap.effective(PA, {"t_amp_k": 1.0}, None)["t"]["min_amplitude_pct"],
      PARAMS["t"]["min_amplitude_pct"])


def mk_stats(mean, sd=2.0, n=200, reg_n=100):
    return {"all": {"n": n, "mean": mean, "sd": sd, "win": 0.6},
            "etf": {"n": n, "mean": mean, "sd": sd, "win": 0.6},
            "stock": {"n": n, "mean": mean, "sd": sd, "win": 0.6},
            "etf|weak": {"n": reg_n, "mean": mean, "sd": sd, "win": 0.6},
            "stock|weak": {"n": reg_n, "mean": mean, "sd": sd, "win": 0.6}}


AUTO = {"generated_at": "x", "data": {"to": DAY}, "regime": {"next": "weak"}, "ks": {"pooled": {}},
        "atr": {}, "stats": {"add_rebound_support": mk_stats(0.8), "reduce_rebound_resist": mk_stats(-0.5),
                             "stop_new_low20": mk_stats(-0.5), "watch_dip_support": mk_stats(0.8, reg_n=5)}}


def ctx(TA=500000, cash=250000, pv=None):
    return {"auto": AUTO, "regime": "weak", "params": PA, "total_assets": TA, "cash": cash,
            "pos_value": pv or {}, "lot": {}, "min_lot": {}}


def cand(t, rule, code="600000", price=10.0, qty=100, avail=1000):
    return {"type": t, "rule": rule, "code": code, "price": price, "qty": qty, "reason": "r",
            "metrics": {"available": avail}, "flags": [], "status": "new"}


c = ap.size_and_ev(cand("add", "add_rebound_support"), ctx())
check("正 EV 加仓：status new、带 ev、金额 ≤ 单笔上限 5%", (c["status"], c["ev"]["ev_pct"] > 0, c["qty"] * 10 <= 25000),
      ("new", True, True))
c = ap.size_and_ev(cand("add", "add_rebound_support"), ctx(pv={"600000": 99000}))
check("仓位已到 20% 上限 → ev_negative", c["status"], "ev_negative")
c = ap.size_and_ev(cand("add", "add_rebound_support"), ctx(cash=500))
check("现金不足一手 → ev_negative（两仓制取消现金底线）", (c["status"], "现金不足" in c["reason"]), ("ev_negative", True))
c = ap.size_and_ev(cand("add", "add_rebound_support"), ctx(pv={"600000": 90000}))
check("距上限 1 万 → 数量不超过空间", c["qty"] * 10 <= 10000 and c["status"] == "new", True)
c = ap.size_and_ev(cand("reduce", "reduce_rebound_resist"), ctx())
check("负 EV 减仓 → ev_negative", c["status"], "ev_negative")
c = ap.size_and_ev(cand("reduce", "reduce_rebound_resist", avail=20000), ctx(pv={"600000": 130000}))
check("仓位 26% 超上限 → 减仓至少减到上限（数量≥3000）且负 EV 也不屏蔽", (c["qty"] >= 3000, c["status"]), (True, "new"))
AUTO["stats"]["t_low_rebound"] = mk_stats(1.0)
c = ap.size_and_ev(cand("t_low", "t_low_rebound", code="00100", price=150.0, qty=10, avail=20), ctx())
check("做T低吸数量不超过可卖数量（港股持 20 股）", c["qty"] <= 20, True)
AUTO["stats"]["reduce_rebound_resist"] = mk_stats(1.0)
c = ap.size_and_ev(cand("reduce", "reduce_rebound_resist", qty=2000, avail=20000), ctx())
check("正 EV 减仓不因凯利放大到规则数量以上", c["qty"], 2000)
AUTO["stats"]["reduce_rebound_resist"] = mk_stats(-0.5)
c = ap.size_and_ev(cand("t_high", "t_high_range_top", qty=100, avail=0), dict(ctx(), auto=dict(AUTO, stats=dict(AUTO["stats"], t_high_range_top=mk_stats(1.0)))))
check("可卖为 0 → 不给卖出数量", (c["status"], c["qty"]), ("ev_negative", 100))
c = ap.size_and_ev(cand("stop", "stop_new_low20", qty=1000), ctx())
check("止损负 EV 不屏蔽，只标注、数量不动", (c["status"], c["qty"], any("止损" in f for f in c["flags"])), ("new", 1000, True))
c = ap.size_and_ev(cand("watch_dip", "watch_dip_support"), dict(ctx(), regime="weak"))
check("当前市场状态样本 5 条 → thin=True", c["ev"]["thin"], True)
_thin = mk_stats(0.8, reg_n=5)
_thin["stock|weak"]["mean"] = -0.8
AUTO["stats"]["watch_dip_support"] = _thin
c = ap.size_and_ev(cand("watch_dip", "watch_dip_support"), ctx())
check("当前市场状态样本 <20、合并看为正：负 EV 只标注不屏蔽", (c["status"], any("仅供参考" in f for f in c["flags"])), ("new", True))
AUTO["stats"]["watch_dip_support"] = mk_stats(-0.8, reg_n=5)
c = ap.size_and_ev(cand("watch_dip", "watch_dip_support"), ctx())
check("当前市场状态样本 <20、合并看也 ≤ 0：屏蔽", (c["status"], "合并各市场状态" in c["reason"]), ("ev_negative", True))
# 最低金额由 EV 反推：ETF 往返固定约 10 元，毛收益 0.5% → 保本约 2000 元、有效金额约 4000 元
_s = mk_stats(0.5)["etf"]
check("保本金额 / 有效金额", (round(ap.amount_for(_s, "510300", "t_high_range_top", 0), -2),
                          round(ap.amount_for(_s, "510300", "t_high_range_top", 1), -2)), (2000, 4000))
check("毛收益为负 → 任何金额都不保本", ap.amount_for(mk_stats(-0.1)["etf"], "510300", "t_high_range_top"), None)
_a = dict(AUTO, stats=dict(AUTO["stats"], t_high_range_top=mk_stats(0.5)))
c = ap.size_and_ev(cand("t_high", "t_high_range_top", code="510300", price=1.0, qty=500, avail=500), dict(ctx(), auto=_a))
check("可卖只有 500 元 < 保本金额 → 屏蔽并写明保本金额",
      (c["status"], "低于保本金额" in c["reason"], c["ev"]["breakeven_yuan"]), ("ev_negative", True, 2000))
c = ap.size_and_ev(cand("t_high", "t_high_range_top", code="510300", price=1.0, qty=1000, avail=20000), dict(ctx(), auto=_a))
check("做T规则数量 1000 元、可卖 2 万 → 按凯利加大（≥ 有效金额、≤ 可卖、≤ 单笔 5%）且 EV>0",
      (c["status"], 3900 <= c["qty"] <= 20000, c["qty"] <= 25000, c["ev"]["ev_pct"] > 0, any("凯利" in f for f in c["flags"])),
      ("new", True, True, True, True))
c = ap.size_and_ev(cand("t_high", "t_high_range_top", code="510300", price=1.0, qty=1000, avail=3000), dict(ctx(), auto=_a))
check("做T按凯利放大但不超过可卖数量", c["qty"] <= 3000, True)
check("load_auto：未启用 → None", ap.load_auto(PARAMS), None)
check("clamp ±30% 与上下限", (cal_clamp := __import__("calibrate").clamp)(1.0, 2.0, 0.2, 1.2, 30), 1.2)
check("clamp 下限", cal_clamp(1.0, 0.1, 0.8, 2.0, 30), 0.8)
check("clamp 目标在范围内", cal_clamp(1.0, 1.1, 0.2, 2.0, 30), 1.1)
_cal = __import__("calibrate")
_day = {"next_close": 10.0, "next_bars": [{"h": 10, "l": 10}], "bars": [],
        "fut_days": [{"high": 10.2, "close": 10.1}, {"high": 10.31, "close": 10.2}] + [{"high": 10.0, "close": 9.9}] * 3}
_c = {"price": 10.0, "rule": "watch_dip_support", "side": 1, "metrics": {}}
check("回放口径：买入类 5 日内碰到 +3% → +3", _cal.outcome(_c, _day, 0, PARAMS), 3.0)
_day["fut_days"] = [{"high": 10.2, "close": 10.1}] * 4 + [{"high": 10.0, "close": 9.8}]
check("回放口径：5 日没碰到 → 第 5 日收盘", round(_cal.outcome(_c, _day, 0, PARAMS), 2), -2.0)
_day["fut_days"] = _day["fut_days"][:3]
check("回放口径：不满 5 日 → 丢弃", _cal.outcome(_c, _day, 0, PARAMS), None)

# 两仓：8 只上限只管长线仓；交易仓受预算约束；碎仓清仓只看长线仓；清碎仓也受 EV 约束
AUTO["stats"]["watch_dip_support"] = mk_stats(0.8)
full = dict(ctx(), n_positions=8, held={"a%d" % i for i in range(8)}, dust=[], sleeves={},
            trade_value=0, trade_budget=100000)
c = ap.size_and_ev(cand("watch_dip", "watch_dip_support", code="300001"), full)
check("长线仓满 8 只 → 交易仓自选首仓照常（不受只数限制）", c["status"], "new")
c = ap.size_and_ev(cand("watch_dip", "watch_dip_support", code="300001"), dict(full, trade_value=99950))
check("交易仓预算已用完 → ev_negative 写明交易仓", (c["status"], "交易仓已到预算上限" in c["reason"]), ("ev_negative", True))
c = ap.size_and_ev(cand("add", "add_rebound_support"), dict(full, trade_value=95000))
check("交易仓剩 5000 → 加仓不超过剩余预算", (c["status"], c["qty"] * 10 <= 5000), ("new", True))
AUTO["stats"]["t_low_rebound"] = mk_stats(1.0)
c = ap.size_and_ev(cand("t_low", "t_low_rebound", qty=100, avail=1000), dict(full, trade_value=200000))
check("做T低吸不占交易仓预算（超预算也照常）", c["status"], "new")
AUTO["stats"]["reduce_rebound_resist"] = mk_stats(0.8)
c = ap.size_and_ev(cand("reduce", "reduce_rebound_resist", qty=100, avail=800),
                   dict(ctx(pv={"600000": 8000}), sleeves={"600000": "core"}))
check("长线仓碎仓减仓 → 数量改为全部可卖", (c["status"], c["qty"]), ("new", 800))
c = ap.size_and_ev(cand("reduce", "reduce_rebound_resist", qty=100, avail=800), ctx(pv={"600000": 8000}))
check("交易仓小仓位减仓 → 不按碎仓清掉", c["qty"] < 800, True)
AUTO["stats"]["reduce_rebound_resist"] = mk_stats(-0.5)
c = ap.size_and_ev(cand("reduce", "reduce_rebound_resist", qty=100, avail=800),
                   dict(ctx(pv={"600000": 8000}), sleeves={"600000": "core"}, n_positions=11))
check("长线仓超只数：负 EV 清碎仓同样屏蔽（不例外）", c["status"], "ev_negative")

# 交易仓 3% 目标价差：买入类上方空间 < 3% 不买；否则带 +3% 止盈价与最长持有天数
c = se.evaluate_position(pos(code="159659", stance="分批加仓", price=1.02, low=1.0, high=1.05, pct=-3.5,
                             tech={"low20": 1.0, "high20": 1.04}), PARAMS, 100000)
a = [x for x in c if x["type"] == "add"]
check("加仓：上方 20 日高距现价 2% < 3% → blocked", (a[0]["status"], "不足 3% 目标价差" in a[0]["reason"]) if a else None,
      ("blocked", True))
c = se.evaluate_position(pos(code="159659", stance="分批加仓", price=1.02, low=1.0, high=1.05, pct=-3.5,
                             tech={"low20": 1.0, "high20": 1.10}), PARAMS, 100000)
a = [x for x in c if x["type"] == "add"]
check("加仓：空间够 → 带止盈 +3% 与持有 5 日", (a[0]["status"], a[0].get("tp_price"), a[0].get("hold_days")) if a else None,
      ("new", 1.051, 5))
c = se.evaluate_position(pos(code="600000", stance="逢反弹减仓 做T 可加仓", price=10.28, low=9.9, high=10.3,
                             prev_close=10.0, available=500, shares=1500, tech={"low20": 9.9, "ma5": 10.3}),
                         PARAMS, 100000, sleeve="core")
check("长线仓：盘中只做T（不出加仓/减仓/创新低止损）",
      sorted({x["type"] for x in c} - {"alert"}), ["t_high"])
check("min_ev_yuan 当前为 20 元", PARAMS["auto"]["sizing"]["min_ev_yuan"], 20)
check("交易仓参数：预算 20%、价差 3%", (PARAMS["sleeves"]["trade"]["budget_frac"], PARAMS["sleeves"]["trade"]["min_move_pct"]),
      (0.2, 3.0))
_core = {"asof": DAY, "sleeves": {"600002": {"sleeve": "core"}}}
check("归属：force > core_auto.json > 默认交易仓",
      (cv.sleeve_of("159813", PARAMS, _core), cv.sleeve_of("600002", PARAMS, _core), cv.sleeve_of("600003", PARAMS, _core)),
      ("trade", "core", "trade"))
_ps = snap([pos(code="600002", market_value=50000), pos(code="600003", market_value=150000)], cash=300000)
_ps["account"]["total_assets"] = 500000
_pf = se.portfolio_info(_ps, PARAMS, core=_core)
check("portfolio：只数只数长线仓；交易仓超预算算出超额",
      (_pf["n_positions"], _pf["trade_value"], _pf["trade_budget"], _pf["trade_room"], "只卖不买" in _pf["note"]),
      (1, 150000, 100000, -50000, True))

# EV 门槛 min_ev_yuan=500：只过滤、不放大数量；止损、仓位超上限的减仓不受限
PA["auto"]["sizing"]["min_ev_yuan"] = 500
AUTO["stats"]["add_rebound_support"] = mk_stats(0.8)
c = ap.size_and_ev(cand("add", "add_rebound_support"), ctx())
check("EV 门槛：正 EV 但 < 500 元 → 屏蔽并写出需要的金额，数量不放大",
      (c["status"], "低于门槛 500" in c["reason"], "需做约" in c["reason"], c["qty"] * 10 <= 25000), ("ev_negative", True, True, True))
AUTO["stats"]["add_rebound_support"] = mk_stats(3.0, sd=6.0)
c = ap.size_and_ev(cand("add", "add_rebound_support"), ctx())
check("EV 门槛：EV ≥ 500 元 → 放行", (c["status"], c["ev"]["ev_yuan"] >= 500), ("new", True))
c = ap.size_and_ev(cand("stop", "stop_new_low20", qty=1000), ctx())
check("EV 门槛：止损不受限", c["status"], "new")
AUTO["stats"]["reduce_rebound_resist"] = mk_stats(0.8)
c = ap.size_and_ev(cand("reduce", "reduce_rebound_resist", avail=20000), ctx(pv={"600000": 130000}))
check("EV 门槛：仓位超上限的减仓不屏蔽、只标注", (c["status"], any("低于门槛" in f for f in c["flags"])), ("new", True))
PA["auto"]["sizing"]["min_ev_yuan"] = 0
AUTO["stats"]["add_rebound_support"] = mk_stats(0.8)

# run 接入：auto 文件里的统计让加仓变 ev_negative，进入 suppressed
_orig = ap.load_auto
ap.load_auto = lambda params, today=None: AUTO
AUTO["stats"]["add_rebound_support"] = mk_stats(-1.0)
sa = snap([pos(code="600001", price=10.0, low=9.7, high=10.3, prev_close=10.4, pct=-3.8, stance="可加仓",
               tech={"low20": 9.7, "ma60": 9.9}, market_value=10000)], cash=300000)
sa["account"]["total_assets"] = 500000
ra = se.run(sa, PA, record=False)
check("run：负 EV 加仓进 suppressed（ev_negative），并输出 auto 信息",
      ([c["type"] for c in ra["suppressed"] if c["status"] == "ev_negative"], ra["auto"]["enabled"]), (["add"], True))
pm = se.premarket_levels(sa, PA)
check("盘前输出动态阈值、归属与可买空间（现金底线 0）",
      ("thresholds" in pm["levels"][0], pm["levels"][0]["sleeve"], pm["cash_room_yuan"]), (True, "trade", 300000.0))
check("run / 盘前输出 portfolio（长线仓只数、交易仓预算）",
      (ra["portfolio"]["n_positions"], pm["portfolio"]["max_positions"], pm["portfolio"]["trade_budget"],
       pm["core"]["enabled"]), (0, 8, 100000, False))
# open_trades：采纳的买入带 tp_price，现价到价 → take_profit；持有满 5 个交易日 → expire
st.append_events("20261023", [{"event": "candidate", "id": "x1", "code": "600001", "name": "t", "type": "watch_dip",
                               "price": 9.0, "qty": 100, "tp_price": 9.27, "hold_days": 5, "time": "10:00"},
                              {"event": "decision", "id": "x1", "decision": "accept"}])
for _d in ("20261026", "20261027", "20261028", "20261029"):
    st.append_events(_d, [{"event": "candidate", "id": "n" + _d, "code": "x", "type": "alert", "time": "10:00"}])
st.append_events("20261029", [{"event": "candidate", "id": "x2", "code": "600001", "name": "t", "type": "add",
                               "price": 9.5, "qty": 100, "tp_price": 9.79, "hold_days": 5, "time": "10:00"},
                              {"event": "decision", "id": "x2", "decision": "accept"}])
_ot = {o["id"]: o["action"] for o in se.open_trades(sa, PA)}
check("open_trades：现价 10 ≥ 止盈 9.27 → take_profit；9.79 也到价", _ot, {"x1": "take_profit", "x2": "take_profit"})
sa["positions"][0]["price"] = 9.2
_ot = {o["id"]: (o["action"], o["held_days"]) for o in se.open_trades(sa, PA)}
check("open_trades：未到价，持有满 5 日 → expire；1 日 → hold", _ot, {"x1": ("expire", 5), "x2": ("hold", 1)})
ap.load_auto = _orig

print("\n== 7. 长线仓持仓成本约束（cost_gate / plan_actions） ==")
_vc = dict(cv.DEFAULTS, _target_weight=0.10)
_be = cv.breakeven(10.0, 1000, "600000")
check("保本价 = 成本 + 卖出费用（1 万元 A 股：佣金 5 + 印花税 5 + 过户费 0.1）", round(_be, 3), 10.01)
check("ETF 保本价只加佣金", cv.breakeven(1.0, 10000, "159813"), 1.0005)
_g = cv.cost_gate({"code": "000657", "held": True, "cost": 100, "shares": 100, "value": 4000, "target": 70}, 40, _vc, 460000)
check("深套 60% 且目标价 < 保本价 → 不加仓", "只做T降成本" in (_g["buy_block"] or ""), True)
_g = cv.cost_gate({"code": "000657", "held": True, "cost": 100, "shares": 100, "value": 4000, "target": 120}, 40, _vc, 460000)
check("深套但目标价高于保本价、首批未建完 → 可加", _g["buy_block"], None)
_g = cv.cost_gate({"code": "600000", "held": True, "cost": 10, "shares": 3000, "value": 28500, "target": 14}, 9.5, _vc, 460000)
check("首批已建完、现价只比成本低 5% → 摊薄不足", "摊薄不足" in (_g["buy_block"] or ""), True)
_g = cv.cost_gate({"code": "600000", "held": True, "cost": 10, "shares": 3000, "value": 27000, "target": 14}, 9.0, _vc, 460000)
check("首批已建完、现价比成本低 10% → 可加", _g["buy_block"], None)
_g = cv.cost_gate({"code": "600000", "held": True, "cost": 10, "shares": 1000, "value": 10300, "target": 10}, 10.3, _vc, 460000)
check("到目标价但 < 保本价×1.05 → 不卖", "持有，不卖" in (_g["sell_block"] or ""), True)
_g = cv.cost_gate({"code": "600000", "held": True, "cost": 10, "shares": 1000, "value": 10300, "target": 10, "review": ["净利同比 -20%"]}, 10.3, _vc, 460000)
check("逻辑复查已触发 → 卖出不受成本约束", _g["sell_block"], None)
_g = cv.cost_gate({"code": "600000", "held": True, "cost": 10, "shares": 1000, "value": 11000, "target": 10}, 11.0, _vc, 460000)
check("现价 ≥ 保本价×1.05 → 可卖", _g["sell_block"], None)
_rows = {"000657": {"code": "000657", "sleeve": "core", "held": True, "cost": 100, "shares": 100, "available": 100,
                    "value": 4000, "price": 40, "target": 70, "zone": "buy", "upside_pct": 75, "review": []},
         "600000": {"code": "600000", "sleeve": "core", "held": True, "cost": 10, "shares": 1000, "available": 1000,
                    "value": 10300, "price": 10.3, "target": 10, "zone": "sell_half", "upside_pct": -3, "review": []}}
cv.plan_actions(_rows, 460000, 200000, PARAMS)
check("plan_actions：深套买入区 → 数量 0、提示只做T", (_rows["000657"]["qty"], "只做T" in _rows["000657"]["action"]), (0, True))
check("plan_actions：卖出区但低于保本价+5% → 不卖", (_rows["600000"]["qty"], "不卖" in _rows["600000"]["action"]), (0, True))

print("\n== 8. ETF 长线评估（etf_value：指数估值法） ==")
import datetime
import math

import etf_value as etfv


def _idx(years, e_fn, pe_fn, end="20261104"):
    """合成指数周线：e_fn(t)/pe_fn(t) 给出第 t 年（0=最早）的盈利与 PE，点位 = 盈利 × PE。"""
    d1 = datetime.datetime.strptime(end, "%Y%m%d")
    n = int(years * 52)
    out = []
    for i in range(n + 1):
        t = i / 52
        d = d1 - datetime.timedelta(days=int((years - t) * 365.25))
        pe = pe_fn(t)
        out.append((d.strftime("%Y%m%d"), e_fn(t) * pe, pe))
    return out


_good_meta = {"scale_yi": 50, "amount_wan": 20000, "fee_pct": 0.5, "price": 1.0, "nav": 1.0, "nav_date": "20261104",
              "premium_pct": 0.0}
_grow = _idx(12, lambda t: 1.1 ** t, lambda t: 16 if t > 11.9 else 20 + 4 * math.sin(t * 2))
_m = etfv.metrics(_grow)
check("稳定增长：10 年盈利年化 ≈ 10%", round(_m["earn_cagr"]), 10)
check("稳定增长：不触发周期修正", _m["cyc_capped"], False)
check("稳定增长 + 估值低于中位数 → 满足准入", etfv.judge(_m, _good_meta)[0], True)
_v = etfv.value(_m, _good_meta, _grow, {}, 1.15)
check("PE 16 vs 中位 19.6 → 空间 ≈ 22%、买入区", (20 < _v["upside_pct"] < 25, _v["zone"]), (True, "buy"))
_cyc = _idx(12, lambda t: 1.0 if t < 11 else 2.2, lambda t: 20 if t < 11 else 20 * 1.6 / 2.2)   # 盈利 ×2.2、价格只涨 1.6 倍
_m = etfv.metrics(_cyc)
check("周期暴增：盈利翻倍触发周期修正，修正 PE 高于名义 PE", (_m["cyc_capped"], _m["pe_norm"] > _m["pe"]), (True, True))
check("周期暴增：名义 PE 14.5 vs 中位 20 看着便宜（空间约 37%），修正后不进买入区",
      (round(20 / _m["pe"] * 100 - 100), etfv.value(_m, _good_meta, _cyc, {}, 1.15)["zone"]), (37, "hold"))
_shr = _idx(12, lambda t: 0.95 ** t, lambda t: 15)
check("盈利长期萎缩 → 不满足准入", any("盈利" in w for w in etfv.judge(etfv.metrics(_shr), _good_meta)[1]), True)
_short = _idx(3, lambda t: 1.1 ** t, lambda t: 20)
check("估值历史 3 年 → 不满足准入", any("估值历史" in w for w in etfv.judge(etfv.metrics(_short), _good_meta)[1]), True)
check("规模 / 成交额不足 → 不满足准入",
      len([w for w in etfv.judge(etfv.metrics(_grow), dict(_good_meta, scale_yi=1.4, amount_wan=482))[1] if "规模" in w or "成交额" in w]), 2)
_m = etfv.metrics(_grow)
_v = etfv.value(_m, dict(_good_meta, price=1.15, premium_pct=15.0), _grow, {}, 1.15)
check("场内溢价 15%：按净值折算分区（仍是买入区）、市价空间更低、买入被溢价闸门拦", (_v["zone"], _v["upside_mkt_pct"] < _v["upside_pct"], bool(_v["entry_block"])), ("buy", True, True))
_rows = {"A": {"code": "A", "etf_ok": True, "zone": "hold", "val_code": "X", "value": 100},
         "B": {"code": "B", "etf_ok": True, "zone": "buy", "val_code": "X", "value": 500},
         "C": {"code": "C", "etf_ok": True, "zone": "sell_half", "val_code": "Y", "value": 100, "upside_pct": -9},
         "D": {"code": "D", "etf_ok": False, "etf_why": ["规模 1 亿 < 20 亿"], "zone": "buy", "val_code": "Z", "value": 100},
         "E": {"code": "E", "etf_ok": False, "etf_why": ["指数盈利同比 -40%"], "zone": "hold", "val_code": "W", "value": 100}}
_a = etfv.assign(_rows, {"E": {"sleeve": "core"}})
check("同一估值指数只放一只（市值大的 B 进长线）", (_a["B"][0], _a["A"][0]), ("core", "trade"))
check("满足条件但估值偏高 → 暂留交易仓", (_a["C"][0], "估值偏高" in _a["C"][1]), ("trade", True))
check("不满足条件 → 交易仓", _a["D"][0], "trade")
check("已在长线仓的不因一次不满足自动移出", _a["E"][0], "core")
_rows = {"159659": {"code": "159659", "kind": "etf", "sleeve": "core", "held": True, "cost": 1.0, "shares": 10000,
                    "available": 10000, "value": 10000, "price": 1.0, "target": 1.4, "zone": "buy", "upside_pct": 40,
                    "review": [], "entry_block": "场内溢价 13.7% > 3%，暂不买"}}
cv.plan_actions(_rows, 460000, 200000, PARAMS)
check("plan_actions：ETF 买入区但溢价过高 → 数量 0", (_rows["159659"]["qty"], "溢价" in _rows["159659"]["action"]), (0, True))

print("\n== 9. 行业景气（industry_heat / 长线仓分档） ==")
import industry_heat as ih

check("百分位：最高 100、最低 0、缺失 0", ih.pct_rank({"a": 1, "b": 2, "c": 3, "d": None}), {"a": 0.0, "b": 50.0, "c": 100.0, "d": 0.0})
_ic = ih.cfg_of({"industry": {"hot_min_score": 55, "cold_max_score": 30, "min_members": 5}})   # 4 个行业样本：门槛取 55
check("cfg_of：未配置的 hot/cold 用默认值", (_ic["hot"]["pe_max"], _ic["hot"]["pe_fair_max"], _ic["cold"]["buy_upside_pct"]), (80, 60, 40))
_ind = {"强": {"members": 50, "rev_cagr3": 20, "np_cagr3": 30, "ytd_np_yoy": 40, "ret_1y": 30, "flow20": 1.0},
        "涨多不赚": {"members": 50, "rev_cagr3": 19, "np_cagr3": -2, "ytd_np_yoy": 600, "ret_1y": 25, "flow20": 0.8},
        "中": {"members": 50, "rev_cagr3": 5, "np_cagr3": 5, "ytd_np_yoy": 5, "ret_1y": 0, "flow20": 0},
        "弱": {"members": 50, "rev_cagr3": -10, "np_cagr3": None, "ytd_np_yoy": None, "ret_1y": -20, "flow20": -2},
        "小": {"members": 2, "small": True, "rev_cagr3": 99, "np_cagr3": 99, "ytd_np_yoy": 99, "ret_1y": 99, "flow20": 9}}
ih.score(_ind, _ic)
check("分档：高分且 3 年、今年净利都增长 → hot", _ind["强"]["tier"], "hot")
check("分档：高分但 3 年净利 CAGR < 0 → 不放宽（neutral）", (_ind["涨多不赚"]["score"] >= 55, _ind["涨多不赚"]["tier"]), (True, "neutral"))
check("分档：低分 → cold", _ind["弱"]["tier"], "cold")
check("分档：成员太少 → 不评分、neutral、不占排名", (_ind["小"]["score"], _ind["小"]["tier"], _ind["强"]["of"]), (None, "neutral", 4))
check("理由文本：高景气写明放宽后的 PE 上限", "PE 上限放宽到 80" in ih.note("强", _ind["强"], _ic), True)
_f0 = {"A": [100, 10, "X"], "B": [100, 10, "X"], "C": [50, 5, "X"]}
_f1 = {"A": [200, 20, "X"], "B": [200, 20, "X"], "D": [1000, 100, "X"]}   # C 只有基期、D 只有当期：不进同口径样本
_fu = ih.fundamentals(_f1, _f0, {"A": [0, 12, "X"], "B": [0, 12, "X"]}, {"A": [0, 10, "X"], "B": [0, 10, "X"]}, min_members=1)
check("同口径加总：3 年翻倍 → CAGR 26%，今年 +20%，样本 2 家", (_fu["X"]["np_cagr3"], _fu["X"]["ytd_np_yoy"], _fu["X"]["n_sample3"]), (26.0, 20.0, 2))
_fu2 = ih.fundamentals(_f1, _f0, {}, {}, min_members=1, years=2)
check("同口径加总：2 年翻倍 → CAGR 41.4%", _fu2["X"]["np_cagr3"], 41.4)
check("报告期：2026-11 基期从 2023 起（2022 疫情年不用）：2025 vs 2023、2026Q3 vs 2025Q3",
      ih.periods(datetime.date(2026, 11, 5)), ("2025-12-31", "2023-12-31", "2026-09-30", "2025-09-30"))
check("报告期：2026-03 年报未出齐，用 2024 年报 vs 2023", ih.periods(datetime.date(2026, 3, 1))[:2], ("2024-12-31", "2023-12-31"))
check("报告期：2027-05 用 2026 vs 2023；2028-05 起滚动 3 年（2027 vs 2024）",
      (ih.periods(datetime.date(2027, 5, 1))[:2], ih.periods(datetime.date(2028, 5, 1))[:2]),
      (("2026-12-31", "2023-12-31"), ("2027-12-31", "2024-12-31")))
check("报告期：2024-05 最近年报就是 2023 → 基期退到 2022（至少 1 年）", ih.periods(datetime.date(2024, 5, 1))[:2], ("2023-12-31", "2022-12-31"))
check("基期标签", ih.span_label(ih.periods(datetime.date(2026, 11, 5))), "23→25")
_c = dict(cv.DEFAULTS)
check("tier_cfg：hot 放宽 PE 上限与合理 PE 上限", (cv.tier_cfg(_c, "hot", _ic)["pe_max"], cv.tier_cfg(_c, "hot", _ic)["pe_fair_max"]), (80, 60))
check("tier_cfg：cold 收紧买入线、neutral 不变", (cv.tier_cfg(_c, "cold", _ic)["buy_upside_pct"], cv.tier_cfg(_c, "neutral", _ic) is _c), (40, True))
check("tier_cfg：enabled=false 不覆盖", cv.tier_cfg(_c, "hot", dict(_ic, enabled=False))["pe_max"], 50)
_q = {"pe_ttm": 57, "cap_yi": 300, "debt_pct": 20, "industry": "半导体"}
_fin = [{"date": "2026-09-30", "roe": 10, "rev_yoy": 20, "np_yoy": 30}, {"date": "2025-12-31", "roe": 15},
        {"date": "2024-12-31", "roe": 14}]
check("筛选：PE 57 中性行业不过、高景气行业过",
      (cv.screen(_q, _fin, {"orgs": 10}, _c)[0], cv.screen(_q, _fin, {"orgs": 10}, cv.tier_cfg(_c, "hot", _ic))[0]), (False, True))
_fc = {"orgs": 10, "eps": {2027: {"eps": 2.0}}, "aim_max": None}
_vh = cv.valuation(100, _fc, [55] * 10, cv.tier_cfg(_c, "hot", _ic), datetime.date(2026, 11, 5))
_vn = cv.valuation(100, _fc, [55] * 10, _c, datetime.date(2026, 11, 5))
check("估值：5 年 PE 中位 55，高景气合理 PE 55（目标 110），中性截到 40（目标 80）", (_vh["target"], _vn["target"]), (110.0, 80.0))
check("估值：低景气买入线 40%：空间 35% → 持有区（中性为买入区）",
      (cv.valuation(100, {"orgs": 10, "eps": {2027: {"eps": 3.375}}}, [40] * 10, cv.tier_cfg(_c, "cold", _ic), datetime.date(2026, 11, 5))["zone"],
       cv.valuation(100, {"orgs": 10, "eps": {2027: {"eps": 3.375}}}, [40] * 10, _c, datetime.date(2026, 11, 5))["zone"]), ("hold", "buy"))
_core7 = {f"60000{i}": {"code": f"60000{i}", "sleeve": "core", "held": True, "cost": 10, "shares": 100, "available": 100,
                        "value": 1000, "price": 10, "target": 11, "zone": "hold", "upside_pct": 10, "review": []} for i in range(7)}
_rows = dict(_core7,
             N1={"code": "N1", "sleeve": "candidate", "screen_ok": True, "price": 10, "zone": "buy", "upside_pct": 90, "industry_tier": "neutral"},
             H1={"code": "H1", "sleeve": "candidate", "screen_ok": True, "price": 10, "zone": "buy", "upside_pct": 40, "industry_tier": "hot"},
             C1={"code": "C1", "sleeve": "candidate", "screen_ok": True, "price": 10, "zone": "buy", "upside_pct": 99, "industry_tier": "cold"})
cv.plan_actions(_rows, 460000, 200000, PARAMS)
check("plan_actions：只剩 1 个空位 → 高景气候选优先（即使空间更小）",
      (_rows["H1"]["qty"] > 0, _rows["N1"]["qty"], "已满" in _rows["N1"]["action"], _rows["C1"]["qty"]), (True, 0, True, 0))
_cs = {"asof": DAY, "params": dict(cv.DEFAULTS), "summary": {"total_assets": 460000},
       "stocks": {"C2": {"code": "C2", "sleeve": "candidate", "screen_ok": True, "price": 10, "target": 13.5, "zone": "hold",
                         "buy_upside_pct": 40, "industry_tier": "cold"},
                  "N2": {"code": "N2", "sleeve": "candidate", "screen_ok": True, "price": 10, "target": 13.5, "zone": "buy",
                         "buy_upside_pct": 30, "industry_tier": "neutral"}}}
_lc = cv.load_core
cv.load_core = lambda params, today=None: _cs
_sx = cv.status(PARAMS, snap={"positions": [], "watchlist": [{"code": "C2", "price": 10}, {"code": "N2", "price": 10}]}, today=DAY)
cv.load_core = _lc
check("status：盘前重新分区沿用行业买入线（低景气 35% 空间不进买入区）", sorted(x["code"] for x in _sx["items"]), ["N2"])

print("\n== 10. 政策信号（policy_signal：快速衰减的政策加分） ==")
import policy_signal as ps
_pc = ps.cfg_of({"policy": {"half_life_days": {"presser": 10}}})
check("cfg_of：未配置项用默认（max 10、讲话半衰期 5 天）", (_pc["max_points"], _pc["half_life_days"]["speech"]), (10, 5))
_e = {"id": "e1", "date": "2026-11-03", "type": "presser", "title": "t", "industries": {"甲": {"s": 3}, "乙": {"s": -1}},
      "stocks": {"S1": {"s": 2, "why": "主业不一致"}}}
check("衰减：当天权重 1、一个半衰期 0.5", (ps.weight(_e, "2026-11-03", _pc), ps.weight(_e, "2026-11-13", _pc)), (1.0, 0.5))
check("衰减：未来事件不计（0）", ps.weight(_e, "2026-11-01", _pc), 0.0)
check("衰减：权重 < 0.05 失效（发布会约 44 天后）", (ps.weight(_e, "2026-12-15", _pc) > 0, ps.weight(_e, "2026-12-18", _pc)), (True, 0.0))
check("衰减：讲话比发布会衰减快", ps.weight(dict(_e, type="speech"), "2026-11-13", _pc) < ps.weight(_e, "2026-11-13", _pc), True)
_r0 = ps.compute([_e], "2026-11-03", _pc)
check("政策分：s=3 当天满分 +10，s=−1 → −3.3", (_r0["industries"]["甲"]["pts"], _r0["industries"]["乙"]["pts"]), (10.0, -3.3))
check("政策分：10 天后减半到 +5", ps.compute([_e], "2026-11-13", _pc)["industries"]["甲"]["pts"], 5.0)
_e2 = dict(_e, id="e2", date="2026-11-10")
check("叠加封顶：两场 +3 叠加仍 ≤ +10", ps.compute([_e, _e2], "2026-11-10", _pc)["industries"]["甲"]["pts"], 10.0)
check("个股政策分：s=2 当天 → 6×2/3 = +4", _r0["stocks"]["S1"]["pts"], 4.0)
check("过期事件不在 active 里", ps.compute([_e], "2027-01-30", _pc)["active"], [])
check("brief：写出类型/日期/强度×权重", ps.brief(_r0["industries"]["甲"]), "政策 +10.0（发布会 11-03 s+3×1.00）")
_bad = ps.validate([{"id": "x", "date": "2026/13/01", "type": "rumor", "title": "t", "industries": {"不存在": {"s": 5}}},
                    {"id": "x", "date": "2026-11-01", "type": "presser", "title": "t"}], known_industries={"甲"})
check("validate：日期/类型/强度/行业名/重复 id 都报错",
      [any(k in b for b in _bad) for k in ("date", "type=rumor", "s=5", "不存在", "id 重复")], [True] * 5)
_ic2 = ih.cfg_of({"industry": {"hot_min_score": 55, "cold_max_score": 30, "min_members": 5}})
_ind2 = {k: dict(v) for k, v in _ind.items()}
_pol = {"industries": {"涨多不赚": {"pts": 10.0, "raw": 3, "items": []}, "弱": {"pts": 10.0, "raw": 3, "items": []},
                       "中": {"pts": -10.0, "raw": -3, "items": []}}}
ih.score(_ind2, _ic2, _pol)
check("叠加：最终分 = 基础分 + 政策分", round(_ind2["弱"]["score"] - _ind2["弱"]["score_base"], 1), 10.0)
check("盈利门槛不变：净利 CAGR < 0 的行业政策加满也不进 hot", _ind2["涨多不赚"]["tier"], "neutral")
_ct = dict(_ic2, cold_max_score=_ind2["弱"]["score_base"] + 5)   # 基础分在低景气线下 5 分，政策 +10 越线
check("政策能跨中性/低景气：弱 cold → neutral，tier_base 仍 cold",
      (lambda v: (v["tier"], v["tier_base"]))(ih._tier(dict(_ind2["弱"]), _ct)), ("neutral", "cold"))
check("负政策分：中 → 低于基础分", _ind2["中"]["score"] < _ind2["中"]["score_base"], True)
check("score 截在 0~100", all(0 <= v["score"] <= 100 for v in _ind2.values() if v.get("score") is not None), True)
_sv = ih.stock_view(_ind2["弱"], {"pts": 6.0, "items": []}, _ic2)
check("stock_view：个股政策分叠加、不改行业行", (_sv["score"] - _ind2["弱"]["score"], _sv is not _ind2["弱"]), (6.0, True))
check("stock_view：无个股政策 → 原样返回", ih.stock_view(_ind2["弱"], None, _ic2) is _ind2["弱"], True)
check("note：显示基础分 + 政策分", "＝基础" in ih.note("弱", _ind2["弱"], _ic2), True)
_ad = cv.advice({"sleeve": "candidate", "screen_ok": True, "zone": "buy", "qty": 0, "policy_pts": 5,
                 "industry_score_base": 26, "industry_tier": "neutral", "industry_tier_base": "cold"}, {})
check("advice：长线仓满 → 候补；政策上调档 + 左侧提示", ("候补" in _ad, "政策上调" in _ad, "左侧" in _ad), (True, True, True))
check("advice：新建给首批股数", cv.advice({"sleeve": "candidate", "screen_ok": True, "zone": "buy", "qty": 200}, {}), "买入（首批 200 股）")
check("advice：交易仓 ETF 清仓区 → 只做T", cv.advice({"kind": "etf", "sleeve": "trade", "zone": "sell_all"}, {}).startswith("只做T"), True)
check("advice：长线仓 ETF 持有区 → 持有（不走交易仓口径）", cv.advice({"kind": "etf", "sleeve": "core", "zone": "hold", "action": "无"}, {}), "持有")
check("advice：交易仓超预算时买入区 → 只卖不买", "只卖不买" in cv.advice({"kind": "etf", "sleeve": "trade", "zone": "buy"}, {"summary": {"trade_over": 1}}), True)

_rp = json.load(open(data_path("rules_params.json"), encoding="utf-8"))
check("roles_of：「目标价前不减仓」不分配减仓", se.roles_of({"stance": "只做T降成本、不加仓；目标价前不减仓", "code": "x"}, _rp), {"t"})
check("roles_of：否定之外仍有减仓词 → 保留减仓", se.roles_of({"stance": "目标价前不减仓，跌破 MA60 逢反弹减", "code": "x"}, _rp), {"reduce"})
check("roles_of：分批加仓 + 不减仓 → 只有加仓", se.roles_of({"stance": "买入区分批加仓；目标价前不减仓", "code": "x"}, _rp), {"add"})

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")
