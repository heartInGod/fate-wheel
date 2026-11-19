#!/bin/bash
# 安装/卸载「休眠兜底」组件：数据采集 + 错过时段补跑 + 定时唤醒。
#
#   ./setup/setup_sleep_guard.sh install     安装（定时唤醒需要 sudo）
#   ./setup/setup_sleep_guard.sh uninstall   卸载
#   ./setup/setup_sleep_guard.sh wake         只设置/续期定时唤醒（需要 sudo）
#   ./setup/setup_sleep_guard.sh status       查看状态
#
# 背景：定时任务调度器（WorkBuddy automation 等）跑在 GUI 进程里，机器合盖睡眠期间
# 到点的任务会被静默跳过且不补跑。本脚本装两个 launchd agent 兜底：
#   com.fatewheel.collect  每个分析时点前 1 分钟采集行情快照并归档
#   com.fatewheel.catchup  每 10 分钟 + 开机时检查缺失时段并补写日志
# 另外用 pmset 在每个分析时点前尝试唤醒机器（尽力而为，合盖深睡时可能失效）。

set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST_SRC="$ROOT/setup/launchd"
PY="$(command -v python3)"

install_agent() {
  local name="$1"
  local dst="$AGENT_DIR/$name.plist"
  cp "$PLIST_SRC/$name.plist" "$dst"
  # plist 里的路径依赖当前用户目录，迁移机器后需要重写
  sed -i '' "s|/Users/fox|$HOME|g" "$dst" 2>/dev/null || true
  launchctl unload "$dst" 2>/dev/null || true
  launchctl load "$dst"
  echo "  ✓ 已加载 $name"
}

uninstall_agent() {
  local name="$1"
  local dst="$AGENT_DIR/$name.plist"
  launchctl unload "$dst" 2>/dev/null || true
  rm -f "$dst"
  echo "  ✓ 已卸载 $name"
}

WAKE_OWNER="fatewheel"
WAKE_DAYS="${WAKE_DAYS:-30}"
# 每个分析时点前 2 分钟
WAKE_TIMES="07:58 09:28 09:38 09:48 09:58 10:08 10:18 10:28 10:38 10:48 10:58 11:08 11:18 11:28 12:58 13:08 13:18 13:28 13:38 13:48 13:58 14:08 14:18 14:28 14:38 14:48 14:58"

# 未来 N 天（仅周一至周五）的所有唤醒时点，格式 "MM/dd/yy HH:mm:ss"，只输出晚于现在的
wake_slots() {
  "$PY" - "$WAKE_DAYS" $WAKE_TIMES <<'PYEOF'
import sys, datetime as dt
days = int(sys.argv[1]); times = sys.argv[2:]
now = dt.datetime.now()
for i in range(days):
    d = now.date() + dt.timedelta(days=i)
    if d.weekday() >= 5:
        continue
    for t in times:
        h, m = map(int, t.split(":"))
        ts = dt.datetime.combine(d, dt.time(h, m))
        if ts > now:
            print(ts.strftime("%m/%d/%y %H:%M:%S"))
PYEOF
}

# 当前已登记、属于本项目的一次性唤醒（从 pmset -g sched 解析）
own_slots() {
  pmset -g sched 2>/dev/null | "$PY" -c '
import re, sys
for line in sys.stdin:
    if "fatewheel" not in line or "wake" not in line.lower():
        continue
    m = re.search(r"(\d\d)/(\d\d)/(\d{2,4}) (\d\d:\d\d:\d\d)", line)
    if m:
        print(f"{m[1]}/{m[2]}/{m[3][-2:]} {m[4]}")'
}

setup_wake() {
  # 注意：合盖 + 无外接显示器时 macOS 会进入硬件深睡，此时 wake 请求可能不生效；
  # 这只是尽力而为，真正兜底的是 catchup 补跑。
  # pmset repeat 只允许一对重复事件，所以分两层：
  #   1) repeat：每个工作日 07:58 唤醒（长期有效）
  #   2) schedule：未来 WAKE_DAYS 天每个时点的一次性唤醒（到期需重跑 ./setup/setup_sleep_guard.sh wake 续期）
  echo "  设置定时唤醒（需要 sudo）…"
  sudo -v || { echo "  ✗ 未获得 sudo 权限，跳过定时唤醒"; return 1; }
  sudo pmset repeat wakeorpoweron MTWRF 07:58:00 || echo "  ✗ repeat 设置失败"
  local s n=0 fail=0
  while IFS= read -r s; do
    [ -n "$s" ] && sudo pmset schedule cancel wake "$s" "$WAKE_OWNER" >/dev/null 2>&1
  done < <(own_slots)
  while IFS= read -r s; do
    if sudo pmset schedule wake "$s" "$WAKE_OWNER" >/dev/null 2>&1; then n=$((n+1)); else fail=$((fail+1)); fi
  done < <(wake_slots)
  echo "  ✓ 每日 07:58 重复唤醒 + 一次性唤醒 $n 个（失败 ${fail}，覆盖未来 ${WAKE_DAYS} 天工作日）"
  echo "    到期前重跑 ./setup/setup_sleep_guard.sh wake 续期；用 'pmset -g sched' 查看"
}

unset_wake() {
  echo "  清除定时唤醒（需要 sudo）…"
  sudo -v || { echo "  ✗ 未获得 sudo 权限"; return 1; }
  sudo pmset repeat cancel
  local s n=0
  while IFS= read -r s; do
    [ -n "$s" ] && sudo pmset schedule cancel wake "$s" "$WAKE_OWNER" >/dev/null 2>&1 && n=$((n+1))
  done < <(own_slots)
  echo "  ✓ 已清除重复唤醒 + $n 个一次性唤醒"
}

case "${1:-install}" in
  install)
    echo "安装休眠兜底组件…"
    mkdir -p "$AGENT_DIR" "$ROOT/logs"
    install_agent com.fatewheel.collect
    install_agent com.fatewheel.catchup
    echo
    setup_wake
    echo
    echo "完成。验证：launchctl list | grep fatewheel"
    ;;
  uninstall)
    echo "卸载休眠兜底组件…"
    uninstall_agent com.fatewheel.collect
    uninstall_agent com.fatewheel.catchup
    echo
    unset_wake
    ;;
  wake)
    setup_wake
    ;;
  status)
    echo "=== launchd agent ==="
    launchctl list | grep -i fatewheel || echo "  (未加载)"
    echo
    echo "=== 定时唤醒 ==="
    pmset -g sched
    last="$(own_slots | tail -1)"
    if [ -n "$last" ]; then echo "  本项目一次性唤醒 $(own_slots | wc -l | tr -d ' ') 个，最后一个：${last}（之后需续期）"
    else echo "  本项目一次性唤醒：无（运行 ./setup/setup_sleep_guard.sh wake 设置）"; fi
    echo
    echo "=== 快照归档 ==="
    ls -la "$ROOT/cache/snapshots/" 2>/dev/null | tail -8 || echo "  (无)"
    echo
    echo "=== 补跑日志 ==="
    tail -5 "$ROOT/logs/catchup.log" 2>/dev/null || echo "  (无)"
    ;;
  *)
    echo "用法: $0 {install|uninstall|wake|status}"
    exit 1
    ;;
esac
