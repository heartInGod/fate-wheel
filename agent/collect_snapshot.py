#!/usr/bin/env python3
"""按计划时点采集行情快照并留档，供错过时段后补跑使用。

背景
----
MiniMax Code 的 cron 调度器跑在 GUI 进程里，机器睡眠（合盖）期间到点的任务会被
**静默跳过且不补跑**。如果只在错过之后补写文字，行情数据已经随内存快照一起丢了，
补出来的结论会失真。

本脚本由系统 launchd 独立于 GUI 定时调用，在每个计划时点把 snapshot 结果按时间戳
归档到 cache/snapshots/，这样错过时段后仍有当时的数据可用。

用法：
  python3 main.py snapshot            # 按当前时刻判断该采哪一类
  python3 main.py snapshot --slot 09:40 --mode intraday
  python3 main.py snapshot --all      # 补采当天所有缺失时点
"""
import datetime
import json
import os
import subprocess
import sys

from paths import ROOT
CACHE = os.path.join(ROOT, "cache")
SNAP_DIR = os.path.join(CACHE, "snapshots")
FETCH = os.path.join(ROOT, "main.py")

# 计划时点：(HH:MM, mode)。与 crons.json / intraday_rules.md 保持一致。
SLOTS = [
    ("09:00", "premarket"),
    ("09:30", "intraday"), ("09:40", "intraday"), ("09:50", "intraday"),
    ("10:00", "intraday"), ("10:10", "intraday"), ("10:20", "intraday"), ("10:30", "intraday"), ("10:40", "intraday"), ("10:50", "intraday"),
    ("11:00", "intraday"), ("11:10", "intraday"), ("11:20", "intraday"), ("11:30", "intraday"),
    ("13:00", "intraday"), ("13:10", "intraday"), ("13:20", "intraday"), ("13:30", "intraday"), ("13:40", "intraday"), ("13:50", "intraday"),
    ("14:00", "intraday"), ("14:10", "intraday"), ("14:20", "intraday"), ("14:30", "intraday"), ("14:40", "intraday"), ("14:50", "intraday"),
    ("15:00", "intraday"),
]


def match_slot(cur):
    """cur 为当天分钟数，返回 (距离, 时点, mode) 或 None。"""
    best = None
    for s, m in SLOTS:
        hh, mm = s.split(":")
        diff = (int(hh) * 60 + int(mm)) - cur      # >0 表示时点还没到
        if -30 <= diff <= 2:                         # 时点间隔 10 分钟，「即将到来」只认 2 分钟内
            key = (0 if diff >= 0 else 1, abs(diff))  # 即将到来的优先
            if best is None or key < best[0]:
                best = (key, s, m)
    return best


def snapshot_path(day, slot):
    return os.path.join(SNAP_DIR, f"{day}_{slot.replace(':', '')}.json")


def run_snapshot(mode):
    """跑一次 snapshot，返回 (ok, payload_or_error)。"""
    try:
        p = subprocess.run([sys.executable, FETCH, "fetch", "snapshot", f"mode={mode}"],
                           cwd=ROOT, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return False, "采集超时(300s)"
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "").strip()[:300]
    try:
        return True, json.loads(p.stdout)
    except json.JSONDecodeError as e:
        return False, f"JSON 解析失败: {e}"


def collect(slot=None, mode=None, day=None, force=False):
    """采集并归档。slot=None 时按当前时间就近匹配（时点前 2 分钟 ~ 时点后 30 分钟）。"""
    now = datetime.datetime.now()
    day = day or now.strftime("%Y%m%d")

    if slot is None:
        # 就近匹配：launchd 在每个时点前 1 分钟触发（如 11:19 采 11:20），
        # 所以优先匹配 2 分钟内即将到来的时点；否则取 30 分钟内刚过去的时点（唤醒晚了）。
        best = match_slot(now.hour * 60 + now.minute)
        if best is None:
            print(f"[{now:%H:%M:%S}] 不在任何采集窗口内，跳过")
            return None
        slot, mode = best[1], best[2]
        print(f"[{now:%H:%M:%S}] 就近匹配到 {slot} ({mode})")

    path = snapshot_path(day, slot)
    if os.path.exists(path) and not force:
        print(f"[{now:%H:%M:%S}] {slot} 已有快照，跳过")
        return path

    # 刚从睡眠唤醒时网络（DNS）可能还没恢复，行情全部失败会得到 market=None 的空快照。
    # 这种情况等一会儿重试；仍失败就不归档，让 catchup 记为「数据缺失」而不是误判休市。
    import time
    for attempt in range(4):
        ok, payload = run_snapshot(mode)
        if ok and payload.get("market"):
            break
        if attempt < 3:
            time.sleep(20)
    if not ok:
        print(f"[{now:%H:%M:%S}] {slot} 采集失败: {payload}")
        return None
    if not payload.get("market"):
        print(f"[{now:%H:%M:%S}] {slot} 行情接口全部失败（可能网络未恢复），不归档："
              f"{(payload.get('errors') or ['?'])[0][:120]}")
        return None

    os.makedirs(SNAP_DIR, exist_ok=True)
    payload["_slot"] = slot
    payload["_mode"] = mode
    payload["_collected_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)
    errs = payload.get("errors") or []
    print(f"[{now:%H:%M:%S}] {slot} 已归档 {os.path.basename(path)}"
          + (f"（{len(errs)} 项接口错误）" if errs else ""))
    return path


def collect_all(day=None):
    """补采当天所有已过时点（用于错过时段后一次性补数据）。"""
    now = datetime.datetime.now()
    day = day or now.strftime("%Y%m%d")
    cur = now.hour * 60 + now.minute
    done = 0
    for s, m in SLOTS:
        hh, mm = s.split(":")
        if int(hh) * 60 + int(mm) > cur:
            continue
        if collect(slot=s, mode=m, day=day) :
            done += 1
    print(f"补采完成，共 {done} 个时点")
    return done


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--all" in args:
        collect_all()
    elif "--slot" in args:
        i = args.index("--slot")
        slot = args[i + 1]
        mode = args[args.index("--mode") + 1] if "--mode" in args else "intraday"
        collect(slot=slot, mode=mode, force="--force" in args)
    else:
        collect(force="--force" in args)
