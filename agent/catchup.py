#!/usr/bin/env python3
"""检测并补齐当天错过的分析时段。

背景
----
MiniMax Code 的 cron 调度器跑在 GUI 进程里，机器睡眠（合盖）期间到点的任务会被
静默跳过且**不补跑**（调度器在应用启动时重算 next_run，直接跳过已过期的触发点）。
本脚本作为独立的兜底：检查当天日志缺了哪些时点，把它们补上。

判定依据
--------
* 计划时点表与 crons.json 一致（09:00 盘前，11-11 前为 08:00；盘中每 10 分钟）。
* 当天日志里已有的 `## HH:MM` 段落视为已覆盖。
* 时点过后 GRACE_MIN 分钟内不判缺失：cron 任务正在跑、还没写日志，此时补写会和正式段落重复。
* 采集快照 cache/snapshots/{yyyyMMdd}_{HHMM}.json 提供该时点的行情数据；
  没有快照时只写「数据缺失」的说明段落，不编造行情结论。

用法：
  python3 main.py catchup            # 检查并补齐
  python3 main.py catchup --dry-run  # 只报告缺哪些

16:10 之后若当天有信号记录但没有 logs/eval/{yyyyMMdd}.md，会补跑 evaluate.py（不发飞书）。
16:20 之后若当天是交易日（有当天快照或信号记录）而 rules_auto.json 还没校准到当天，补跑 calibrate.py
（每天只尝试一次，标记在 cache/calib/attempt_{yyyyMMdd}）。
盘中时点有快照时，还会用 signals_engine 回放出规则候选，记入 logs/signals/（source=catchup，
未经大模型判断、不发飞书），供收盘评估统计「规则单独跑」的效果。
"""
import datetime
import json
import os
import re
import sys

from paths import ROOT
LOG_DIR = os.path.join(ROOT, "logs")
SNAP_DIR = os.path.join(ROOT, "cache", "snapshots")

# (时点, 类型)：premarket=盘前，intraday=盘中
SLOTS = [
    ("09:00", "premarket"),
    ("09:30", "intraday"), ("09:40", "intraday"), ("09:50", "intraday"),
    ("10:00", "intraday"), ("10:10", "intraday"), ("10:20", "intraday"), ("10:30", "intraday"), ("10:40", "intraday"), ("10:50", "intraday"),
    ("11:00", "intraday"), ("11:10", "intraday"), ("11:20", "intraday"), ("11:30", "intraday"),
    ("13:00", "intraday"), ("13:10", "intraday"), ("13:20", "intraday"), ("13:30", "intraday"), ("13:40", "intraday"), ("13:50", "intraday"),
    ("14:00", "intraday"), ("14:10", "intraday"), ("14:20", "intraday"), ("14:30", "intraday"), ("14:40", "intraday"), ("14:50", "intraday"),
    ("15:00", "intraday"),
]

# 2026-11-04 14:40 起盘中由每 15 分钟改为每 10 分钟；当天此前按旧表运行，
# 只检查新旧两表共有的整 15 分时点，免得把 09:40、10:10 这类当时不存在的时点补写成「数据缺失」。
SCHEDULE_SWITCH = ("20261104", "14:40")

GRACE_MIN = 7  # cron 单次分析约 1.5~3 分钟，留足余量；须小于时点间隔 10 分钟
GRACE_PREMARKET = 30  # 盘前要拉快照 + 搜索 + 核实前提，常要 10~25 分钟

_SEC = re.compile(r"^##\s+(\d{1,2}:\d{2})")


def log_path(day):
    return os.path.join(LOG_DIR, f"{day}.md")


def covered_slots(path):
    """日志里已记录的时点集合。"""
    if not os.path.exists(path):
        return set()
    out = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _SEC.match(line)
            if m:
                out.add(m.group(1))
    return out


def snap_path(day, slot):
    return os.path.join(SNAP_DIR, f"{day}_{slot.replace(':', '')}.json")


def load_snapshot(day, slot):
    p = snap_path(day, slot)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def summarize(snap):
    """把一个快照压缩成日志里的几行要点。"""
    if not snap:
        return None
    m = snap.get("market") or {}
    idx = snap.get("indices") or {}
    lines = []
    a = [i for i in idx if i.get("symbol") == "sh000001"]
    hk = [i for i in idx if i.get("symbol") == "hkHSI"]
    if a:
        lines.append(f"上证 {a[0].get('price')}（{a[0].get('pct')}%）")
    if hk:
        lines.append(f"恒指 {hk[0].get('price')}（{hk[0].get('pct')}%）")
    worst, worst_p = None, 0
    for p in snap.get("positions") or []:
        pct = p.get("pct")
        if pct is not None and pct < worst_p:
            worst, worst_p = p, pct
    if worst:
        lines.append(f"持仓中跌幅最大：{worst.get('name')} {worst.get('pct')}%"
                     f"（主力 {(worst.get('fund_flow_today') or {}).get('main_net_pct', 'n/a')}）")
    errs = snap.get("errors") or []
    return "；".join(lines), errs


def engine_candidates(snap):
    """用规则引擎回放该时点，候选记入 logs/signals/（source=catchup），返回一句摘要。失败不影响补跑。"""
    try:
        import signal_store as st
        import signals_engine as se
        res = se.run(snap, st.load_params(), record=True, source="catchup")
    except Exception as e:  # noqa: BLE001
        return f"规则引擎失败（{e}）"
    c = res.get("candidates") or []
    if not c:
        return "无"
    return "；".join(f"{x['label']}{x['name']} {x['price']}" for x in c)


def append_paragraph(path, slot, kind, snap, mkt_open):
    """向日志追加一个补跑段落。"""
    text = []
    title = "盘前分析" if kind == "premarket" else ("收盘分析" if slot == "15:00" else "盘中分析")
    text.append(f"\n## {slot} {title}（补跑）")

    if not snap:
        text.append(f"- 结论：数据缺失（该时点无采集快照，机器休眠导致任务与数据均未生成）")
        text.append("- 飞书：未发送（无数据，不做提醒）")
    elif not mkt_open:
        text.append("- 结论：休市（当日不开市，跳过分析）")
        text.append("- 飞书：未发送（休市）")
    else:
        summary, errs = summarize(snap)
        text.append(f"- 结论：补跑该时点分析 —— {summary}")
        text.append("- 说明：本段由 catchup.py 自动补写，行情数据取自该时点采集的快照"
                    f"（{snap.get('_collected_at', '未知时间')}）；"
                    "未重新抓取新闻与公告，未经大模型判断。")
        if kind == "intraday":
            text.append(f"- 规则候选（catchup，未判断、未发送，供收盘评估）：{engine_candidates(snap)}")
        if errs:
            text.append(f"- 数据缺失：{len(errs)} 项接口错误（{'；'.join(str(e) for e in errs[:3])}）")
        text.append("- 飞书：未发送（补跑只记录数据，不做提醒；如需当日结论请查看盘中任务记录）")

    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(text) + "\n")


def ensure_log_file(path, day):
    if not os.path.exists(path):
        os.makedirs(LOG_DIR, exist_ok=True)
        d = datetime.datetime.strptime(day, "%Y%m%d").strftime("%Y-%m-%d")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {d} 持仓分析日志\n")
        return True
    return False


EVAL_AFTER = 16 * 60 + 10   # 16:00 评估任务 + 10 分钟余量


def catchup_eval(day, cur, dry):
    """16:00 收盘评估若因休眠没跑：有当日信号、没有评估报告时补跑 evaluate.py（只出报告，不发飞书）。"""
    if cur < EVAL_AFTER:
        return
    sig = os.path.join(LOG_DIR, "signals", f"{day}.jsonl")
    rep = os.path.join(LOG_DIR, "eval", f"{day}.md")
    if not os.path.exists(sig) or os.path.exists(rep):
        return
    if dry:
        print(f"  - 收盘评估缺失：将补跑 evaluate.py（{day}）")
        return
    try:
        import evaluate
        evaluate.main(["--date", day, "--quiet"])
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 补跑评估失败：{e}")
        return
    if os.path.exists(rep):
        ensure_log_file(log_path(day), day)
        with open(log_path(day), "a", encoding="utf-8") as f:
            f.write(f"\n## 16:00 建议评估（补跑）\n- 说明：16:00 评估任务未运行（机器休眠），由 catchup.py 补跑 evaluate.py；"
                    f"未写复盘结论，未发飞书。\n- 报告：logs/eval/{day}.md\n")
        print(f"  ✓ 已补跑收盘评估 logs/eval/{day}.md")


CALIB_AFTER = 16 * 60 + 20   # 16:00 任务里 calibrate 约 1.5 分钟，留足余量


def catchup_calib(day, cur, dry):
    """16:00 任务没跑 calibrate.py（休眠或失败）时补跑，更新次日的动态阈值。"""
    if cur < CALIB_AFTER:
        return
    traded = (os.path.exists(os.path.join(LOG_DIR, "signals", f"{day}.jsonl"))
              or os.path.exists(os.path.join(ROOT, "cache", "snapshots", f"{day}_1500.json")))
    if not traded:
        return
    try:
        import auto_params as ap
        import signal_store as st
        params = st.load_params()
        if not ap.auto_cfg(params).get("enabled"):
            return
        with open(ap.auto_path(params), encoding="utf-8") as f:
            done = str((json.load(f).get("data") or {}).get("to")) == day
    except (OSError, json.JSONDecodeError):
        done = False
    mark = os.path.join(ROOT, "cache", "calib", f"attempt_{day}")
    if done or os.path.exists(mark):
        return
    if dry:
        print(f"  - 动态阈值未校准到 {day}：将补跑 calibrate.py")
        return
    os.makedirs(os.path.dirname(mark), exist_ok=True)
    open(mark, "w").close()
    try:
        import calibrate
        auto, _ = calibrate.calibrate()
        line = calibrate.summary_line(auto)
    except BaseException as e:  # noqa: BLE001  (calibrate 无数据时 SystemExit)
        print(f"  ✗ 补跑校准失败：{e}")
        return
    ensure_log_file(log_path(day), day)
    with open(log_path(day), "a", encoding="utf-8") as f:
        f.write(f"\n## 参数校准（补跑）\n- 说明：16:00 任务未运行 calibrate.py，由 catchup.py 补跑；未发飞书。\n"
                f"- 结果：{line}\n- 报告：logs/calib/{day}.md\n")
    print(f"  ✓ 已补跑校准：{line}")


def main():
    dry = "--dry-run" in sys.argv
    now = datetime.datetime.now()
    day = now.strftime("%Y%m%d")
    cur = now.hour * 60 + now.minute
    catchup_eval(day, cur, dry)
    catchup_calib(day, cur, dry)

    path = log_path(day)
    have = covered_slots(path)
    missing = []
    for slot, kind in SLOTS:
        hh, mm = slot.split(":")
        if int(hh) * 60 + int(mm) + (GRACE_PREMARKET if kind == "premarket" else GRACE_MIN) > cur:
            continue  # 还没到，或 cron 任务可能还在跑
        if slot in have or (kind == "premarket" and have & {"08:00", "09:00"}):
            continue
        if (day, slot) < SCHEDULE_SWITCH and int(mm) % 15:
            continue  # 改频率前的旧时点表里没有这个时点
        missing.append((slot, kind))

    if not missing:
        print(f"[{now:%H:%M:%S}] {day} 全部时点已覆盖，无需补跑")
        return 0

    print(f"[{now:%H:%M:%S}] {day} 缺失 {len(missing)} 个时点: "
          + ", ".join(s for s, _ in missing))
    if dry:
        for slot, kind in missing:
            snap = load_snapshot(day, slot)
            print(f"  - {slot} ({kind}) 快照: {'有' if snap else '无'}")
        return 0

    ensure_log_file(path, day)
    for slot, kind in missing:
        snap = load_snapshot(day, slot)
        if snap and not snap.get("market"):
            snap = None  # 行情接口全部失败的空快照（如唤醒时网络未恢复）→ 按数据缺失处理，不能当作休市
        mkt_open = True
        if snap:
            m = snap["market"]
            now_s = str(m.get("now") or "")
            # 9:15 集合竞价前，指数最新行情仍是上一交易日，a_open_today 恒为 false（11-11 08:00 补跑误写「休市」）；
            # 盘前快照改按是否工作日判断（法定节假日无法识别，以盘前任务联网核实为准）
            if len(now_s) >= 16 and now_s[11:16] < "09:15":
                mkt_open = datetime.datetime.strptime(now_s[:10], "%Y-%m-%d").weekday() < 5
            else:
                mkt_open = bool(m.get("a_open_today"))
        append_paragraph(path, slot, kind, snap, mkt_open)
        print(f"  ✓ 已补写 {slot}")

    print(f"[{now:%H:%M:%S}] 补跑完成，写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
