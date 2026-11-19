#!/bin/bash
# fate_wheel 一键配置脚本（迁移到新电脑 / 新目录后运行）
# 自动识别平台：macOS（Darwin）与 Linux / WSL。
# 依赖安装按平台选包管理器（macOS: brew，Linux/WSL: apt-get）；
# macOS 专属的休眠兜底（launchd + pmset）在其他平台自动跳过，改为提示手动补跑。
#
# 用法：
#   ./setup.sh                    基础配置（检查环境、建目录、修正路径、安装 fate 命令、生成定时任务指令）
#   ./setup.sh --check            另外跑一次盘中 snapshot + 生成看板，验证数据源可用
#   ./setup.sh --test-notify     另外给自己发一条通知测试消息（走当前配置的渠道）
#   ./setup.sh --channel wechat  把通知渠道切成 wechat（默认）或 feishu
#   ./setup.sh --open-id ou_xxx  飞书渠道的接收人 open_id（切到飞书时才需要）
#   ./setup.sh --install-deps    缺 python3 / node / lark-cli 时用系统包管理器自动安装
#
# 脚本可重复运行，已有的数据文件（data/portfolio/*.csv、data/report/、logs/ 等）不会被覆盖。

set -u
PROJECT="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
CHECK=0; TEST_NOTIFY=0; INSTALL_DEPS=0; NEW_OPEN_ID=""; NEW_CHANNEL=""

# ---- 平台识别：macos / wsl / linux / unknown ----
PLATFORM="unknown"
case "$(uname -s)" in
  Darwin) PLATFORM="macos" ;;
  Linux)
    if grep -qi microsoft /proc/version 2>/dev/null; then PLATFORM="wsl"
    else PLATFORM="linux"; fi
    ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --test-notify|--test-feishu) TEST_NOTIFY=1 ;;   # --test-feishu 保留为兼容别名
    --install-deps) INSTALL_DEPS=1 ;;
    --open-id) NEW_OPEN_ID="${2:-}"; shift ;;
    --channel) NEW_CHANNEL="${2:-}"; shift ;;
    -h|--help) awk 'NR>1 && /^set -u/{exit} NR>1' "$0"; exit 0 ;;
    *) echo "未知参数：$1"; exit 1 ;;
  esac
  shift
done

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; WARNINGS=$((WARNINGS+1)); }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; WARNINGS=$((WARNINGS+1)); }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
WARNINGS=0
cd "$PROJECT" || exit 1
echo "项目目录：$PROJECT"

# ---------------------------------------------------------------- 1. 环境
step "1. 检查运行环境（平台：$PLATFORM）"
if ! command -v python3 >/dev/null 2>&1; then
  if [ $INSTALL_DEPS = 1 ]; then
    case "$PLATFORM" in
      macos) command -v brew >/dev/null 2>&1 && brew install python ;;
      linux|wsl)
        if command -v apt-get >/dev/null 2>&1; then
          sudo apt-get update -qq && sudo apt-get install -y python3
        else
          echo "找不到 apt-get，请手动安装 python3" >&2
        fi
        ;;
    esac
  fi
fi
if command -v python3 >/dev/null 2>&1; then
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    ok "python3 $(python3 -c 'import platform;print(platform.python_version())')（只用标准库，无需 pip 安装）"
  else
    fail "python3 版本过低，需要 3.9+"
  fi
else
  fail "未找到 python3，请运行 brew install python（或加 --install-deps）"
fi

# 通知：默认走微信个人号桥接（纯 HTTP，无额外依赖）；lark-cli 仅飞书渠道需要。
if command -v lark-cli >/dev/null 2>&1; then
  ok "lark-cli 已安装（飞书渠道可用）"
  if lark-cli config show >/dev/null 2>&1; then
    ok "lark-cli 已配置飞书应用"
  else
    warn "lark-cli 未配置飞书应用（如需飞书渠道：lark-cli config init，机器人需 im:message 权限）"
  fi
else
  echo "  提示：默认通知渠道为微信个人号（无需 lark-cli）；需要飞书时再 npm install -g @larksuite/cli"
fi
if [ -f "$PROJECT/config/notify.json" ]; then
  ok "通知配置 config/notify.json 已存在"
else
  if [ -f "$PROJECT/config/notify.example.json" ]; then
    cp "$PROJECT/config/notify.example.json" "$PROJECT/config/notify.json"
    ok "已从模板创建 config/notify.json（默认微信渠道，可按需修改）"
  fi
fi

# ---------------------------------------------------------------- 2. 目录与数据文件
step "2. 目录与数据文件"
for d in logs cache profiles data/portfolio data/announcements data/research data/report; do mkdir -p "$d"; done
ok "data/report/ logs/ cache/ profiles/ data/portfolio/ data/announcements/ data/research/"

BOM=$'\xEF\xBB\xBF'
make_csv() {  # 文件不存在时写入带 BOM 的表头
  if [ -f "$1" ]; then ok "$1 已存在，保持不动"
  else printf '%s%s\n' "$BOM" "$2" > "$1"; warn "$1 不存在，已创建空表头，请填写：$2"; fi
}
make_csv data/portfolio/holdings.csv   "名称,代码,市场,持仓数量,可用数量,成本价,币种,当前定位"
make_csv data/portfolio/account.csv    "更新日期,总资产,总市值,可用资金,浮动盈亏,仓位"
make_csv data/portfolio/operations.csv "日期,时间,操作,名称,代码,数量,价格,备注"

# ---------------------------------------------------------------- 3. 路径与通知配置
step "3. 修正项目路径与通知配置"
CRONS="$PROJECT/setup/crons.json"
if [ ! -f "$CRONS" ]; then
  fail "缺少 setup/crons.json（定时任务定义），无法生成定时任务指令"
else
  python3 - "$PROJECT" "$NEW_OPEN_ID" "$NEW_CHANNEL" <<'PY'
import json, re, sys, pathlib
project, new_id, new_channel = sys.argv[1], sys.argv[2], sys.argv[3]
root = pathlib.Path(project)
crons_p = root / "setup" / "crons.json"
data = json.loads(crons_p.read_text("utf-8"))
old_project = data["source_project"]
rules_p = root / "intraday_rules.md"
rules = rules_p.read_text("utf-8")
# 规则文件里实际写的工作目录（以防和 crons.json 记录的不一致）
m = re.search(r"^工作目录：(.+)$", rules, re.M)
old_paths = {old_project} | ({m.group(1).strip()} if m else set())
changed = []
for op in old_paths:
    if op != project:
        rules = rules.replace(op, project)
        for t in data["tasks"]:
            t["prompt"] = t["prompt"].replace(op, project)
        changed.append(f"路径 {op} → {project}")
data["source_project"] = project
rules_p.write_text(rules, "utf-8")
crons_p.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
for c in changed:
    print(f"  \033[32m✓\033[0m 已替换{c}（intraday_rules.md、setup/crons.json）")
if not changed:
    print(f"  \033[32m✓\033[0m 项目路径无需修改（{project}）")

# 通知配置：渠道 / 飞书 open_id 都写到 config/notify.json（模板已在第 1 步复制）
notify_p = root / "config" / "notify.json"
if notify_p.exists():
    n = json.loads(notify_p.read_text("utf-8"))
    nchanged = []
    if new_channel in ("wechat", "feishu") and n.get("channel") != new_channel:
        n["channel"] = new_channel; nchanged.append(f"通知渠道 → {new_channel}")
    if new_id and n.setdefault("feishu", {}).get("open_id") != new_id:
        n["feishu"]["open_id"] = new_id; nchanged.append(f"飞书 open_id → {new_id}")
    if nchanged:
        notify_p.write_text(json.dumps(n, ensure_ascii=False, indent=2), "utf-8")
        for c in nchanged:
            print(f"  \033[32m✓\033[0m {c}（config/notify.json）")
PY
fi

# ---------------------------------------------------------------- 4. fate 命令
step "4. 安装 fate 命令"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/fate" <<EOF
#!/bin/bash
# fate wheel：刷新并在浏览器打开 fate_wheel 持仓看板（由 setup.sh 生成）
PROJECT="$PROJECT"
FATE_PLATFORM="$PLATFORM"
EOF
cat >> "$BIN_DIR/fate" <<'EOF'

usage() {
  cat <<USAGE
用法：
  fate wheel            用本地缓存重新生成看板数据并直接打开看板（约 1~3 秒；双击 file:// 即可，无需服务）
  fate wheel -f         先联网重抓盘中快照 + 日 K / 技术位（限速下约 3~5 分钟）再生成并打开
  fate wheel -o         不重新生成，直接打开现有看板
  fate serve            可选：启动本地 HTTP 服务（仅在只有 JSON、没有 JS 的历史数据下才需要）
USAGE
}

PORT=8765
open_board() {
  local target="$PROJECT/dashboard.html"
  case "$FATE_PLATFORM" in
    macos)
      if command -v open >/dev/null 2>&1; then open "$target"; return 0; fi
      ;;
    wsl)
      # 交给 Windows 默认浏览器；WSL 里的 /usr/bin/open 不是浏览器命令，不能用
      if command -v cmd.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
        local wt="$(wslpath -w "$target")"
        cmd.exe /c start "" "$wt" >/dev/null 2>&1 && return 0
      fi
      ;;
  esac
  if [ "$FATE_PLATFORM" != "macos" ] && command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$target"; return 0
  fi
  if command -v wslview >/dev/null 2>&1; then wslview "$target"; return 0; fi
  echo "找不到打开浏览器的命令，请手动打开： $target" >&2
  return 1
}
serve() {
  cd "$PROJECT" || exit 1
  if command -v python3 >/dev/null 2>&1; then
    echo "看板地址： http://localhost:$PORT/dashboard.html"
    exec python3 -m http.server "$PORT" --bind 127.0.0.1
  else
    echo "找不到 python3，无法启动 HTTP 服务" >&2; exit 1
  fi
}

case "$1" in
  wheel)
    cd "$PROJECT" || exit 1
    case "$2" in
      -o|--open) ;;
      -f|--fresh) python3 main.py dashboard fresh=true || echo "数据生成失败，尝试打开旧看板" ;;
      "") python3 main.py dashboard || echo "数据生成失败，尝试打开旧看板" ;;
      *) usage; exit 1 ;;
    esac
    open_board
    ;;
  serve) serve ;;
  *) usage; exit 1 ;;
esac
EOF
chmod +x "$BIN_DIR/fate"
ok "$BIN_DIR/fate"
case ":$PATH:" in
  *":$BIN_DIR:"*) ok "$BIN_DIR 已在 PATH 中" ;;
  *)
    configured=""
    for f in "$HOME/.zshenv" "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
      if [ -f "$f" ] && grep -q "$BIN_DIR" "$f"; then configured="$f"; break; fi
    done
    if [ -n "$configured" ]; then
      ok "PATH 已在 $configured 中配置（新开终端生效）"
    else
      case "${SHELL:-}" in
        */zsh)  target="$HOME/.zshrc" ;;
        */bash) target="$HOME/.bashrc" ;;
        *)      target="$HOME/.profile" ;;
      esac
      [ "$PLATFORM" = "macos" ] && target="$HOME/.zshenv"
      printf '\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$target"
      ok "已把 $BIN_DIR 加入 $target，新开终端生效"
    fi ;;
esac

# ---------------------------------------------------------------- 5. 自检
step "5. 自检"
if N=$(python3 main.py fetch list 2>/dev/null | grep -c '^|'); then
  ok "fetch_data.py 可运行（接口表 $N 行）"
else
  fail "python3 main.py fetch list 运行失败"
fi
if [ $CHECK = 1 ]; then
  echo "  运行盘中 snapshot（约 10~30 秒）…"
  if python3 main.py fetch snapshot mode=intraday > cache/setup_check.json 2>/dev/null; then
    ERR=$(python3 -c "import json;d=json.load(open('cache/setup_check.json'));print(len(d.get('errors',[])))" 2>/dev/null)
    ok "snapshot 成功，errors ${ERR:-?} 项（详见 cache/setup_check.json）"
  else
    fail "snapshot 失败，检查网络"
  fi
  python3 main.py dashboard >/dev/null 2>&1 && ok "看板数据 dashboard.json / dashboard.js 已生成（可直接打开根目录 dashboard.html 查看）" || fail "dashboard 数据生成失败"
fi
if [ $TEST_NOTIFY = 1 ]; then
  if python3 main.py notify --test; then
    ok "通知测试消息已按当前渠道发送（python3 main.py notify --status 可查看渠道）"
  else
    fail "通知测试发送失败：微信渠道请确认 Windows 桥接已启动；飞书渠道检查 lark-cli auth status"
  fi
fi

# ---------------------------------------------------------------- 6. 定时任务（WorkBuddy）
step "6. 定时任务（在 WorkBuddy 中安装）"
OUT="$PROJECT/setup/WORKBUDDY_SETUP_PROMPT.md"

# 先确保 WorkBuddy 任务定义已从 crons.json 派生（幂等：已存在则跳过生成）
if [ ! -f "$PROJECT/setup/workbuddy/automations.json" ]; then
  python3 "$PROJECT/setup/workbuddy/_generate.py" >/dev/null 2>&1 || warn "workbuddy 任务定义生成失败，请检查 setup/workbuddy/_generate.py"
fi

python3 - "$PROJECT" > "$OUT" <<'PY'
import json, sys, os, re
project = sys.argv[1]
# WorkBuddy 调度器在 Windows 侧，看项目用的是 W:/ 挂载路径（不是 WSL 的 \\wsl.localhost 路径）。
# 从 WSL 路径 /home/fox/... 推出 WorkBuddy 侧的 W:/home/fox/...：优先用环境变量覆盖，
# 否则按常见布局把 /home/... 映射为 W:/home/...。
win_project = os.environ.get("FATE_WIN_PROJECT", "")
if not win_project:
    m = re.match(r"^(/home/[^/]+/.*)$", project)
    win_project = "W:" + m.group(1) if m else project
d = json.load(open(f"{project}/setup/workbuddy/automations.json", encoding="utf-8"))
rows = "\n".join(
    f"  - {a['name']}: `{a['rrule']}`"
    for a in d["automations"]
)
print(f"""请在 WorkBuddy 里为持仓盯盘项目重建定时任务。

## 背景
项目原先把定时任务托管在 MiniMax Code，现 MiniMax Code 已停用，改为 WorkBuddy 原生定时任务（automation）。
任务定义与逐条 prompt 已在项目内生成好，不要自己重新设计调度；fate-wheel 之外的任务一律不要动。

## 要创建的任务（共 {d['automation_count']} 条，全部 ACTIVE）
{rows}

说明：WorkBuddy 的 RRULE 不支持一条规则里写多个时点（BYHOUR/BYMINUTE 只能是单个整数），
所以原来"每 10 分钟、覆盖多个小时"的盘中任务，按"分钟偏移"合并成了 6 条（:00/:10/:20/:30/:40/:50），
每条每小时触发一次，由 prompt 里的【时点判断】还原原来只在交易时段、每 10 分钟分析的节奏。
15:00 收盘并入 :00 那一条（按 intraday_rules.md 第 7 条写收盘小结）。

## 执行步骤
1. 先用 automation 列表工具查看现有任务；名称以「【fate-wheel】」开头的旧任务全部删除，避免重复运行。
   注意只删 fate-wheel 前缀的，不要动其它任务。
2. 读取 `{win_project}/setup/workbuddy/automations.json`，对 automations 里的每一条：
   - 用 automation 创建工具（recurring）创建，参数取文件里的 `name`、`rrule`、`status`；
   - `prompt` 由你现场生成，模板固定为下面这段（把 {{文件}} 换成该条的 `prompt_file`）：

     请先用 Read 工具完整读取 `{win_project}/setup/workbuddy/prompts/{{文件}}`，
     然后严格逐条执行其中全部指令，不得省略。该文件已包含运行环境说明和任务正文。

   - 工作目录设为 `{win_project}`（Windows 路径）。
3. 创建完核对：共 {d['automation_count']} 条、都是 ACTIVE，并列出各条下次运行时间。

## 环境说明
- 项目代码与数据在 WSL：`{d['wsl_project']}`；WorkBuddy 调度器在 Windows 侧，
  所以每条 prompt 都要求 agent 用 `wsl.exe -e bash -lc` 执行项目命令。
- 通知统一走 `python3 main.py notify`，渠道在 config/notify.json 配置（默认 wechat，走 Windows 侧微信个人号桥接）。
  桥接未启动时任务会在日志注明并跳过通知，不影响分析与记录；详见 setup/wechat_bridge/README.md。
- 时区：WorkBuddy RRULE 用系统本地时间（Asia/Shanghai），无需额外设置。""")
PY
ok "已生成 setup/WORKBUDDY_SETUP_PROMPT.md"
if command -v pbcopy >/dev/null 2>&1; then
  pbcopy < "$OUT" && ok "指令已复制到剪贴板"
elif command -v clip.exe >/dev/null 2>&1; then
  clip.exe < "$OUT" && ok "指令已复制到 Windows 剪贴板"
elif command -v xclip >/dev/null 2>&1; then
  xclip -selection clipboard < "$OUT" && ok "指令已复制到剪贴板（xclip）"
fi
echo "  → 在 WorkBuddy 里新建会话，粘贴下面这段指令（或直接用 WORKBUDDY_SETUP_PROMPT.md）："
echo "  ------------------------------------------------------------"
sed 's/^/  /' "$OUT"
echo "  ------------------------------------------------------------"

step "7. 漏跑补兜（sleep guard，仍建议）"
if [ "$PLATFORM" = "macos" ]; then
  if launchctl list 2>/dev/null | grep -q "com.fatewheel.catchup"; then
    ok "已安装（com.fatewheel.collect / com.fatewheel.catchup）"
  else
    warn "未安装。机器合盖睡眠时定时任务可能被跳过且不补跑。"
    echo "  → 执行 ./setup/setup_sleep_guard.sh install  安装（定时唤醒那步需要 sudo 密码；之后每 30 天跑一次 ./setup/setup_sleep_guard.sh wake 续期）"
    echo "  → 装完用 ./setup/setup_sleep_guard.sh status  查看状态"
  fi
else
  ok "当前平台 $PLATFORM：macOS launchd / pmset 休眠兜底不适用，已跳过"
  echo "  → 睡眠后若日志缺失，手动补跑：python3 main.py catchup、python3 main.py snapshot"
fi

step "完成"
if [ $WARNINGS = 0 ]; then ok "全部通过"; else warn "共 $WARNINGS 项需要处理（见上方 ! / ✗）"; fi
echo "  打开看板：fate wheel"
