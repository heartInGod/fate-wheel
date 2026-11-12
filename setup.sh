#!/bin/bash
# fate_wheel 一键配置脚本（迁移到新电脑 / 新目录后运行）
#
# 用法：
#   ./setup.sh                    基础配置（检查环境、建目录、修正路径、安装 fate 命令、生成定时任务指令）
#   ./setup.sh --check            另外跑一次盘中 snapshot + 生成看板，验证数据源可用
#   ./setup.sh --test-feishu      另外给自己发一条飞书测试消息
#   ./setup.sh --open-id ou_xxx   飞书接收人换成另一个 open_id（换了飞书应用时需要）
#   ./setup.sh --install-deps     缺 python3 / node / lark-cli 时用 Homebrew、npm 自动安装
#
# 脚本可重复运行，已有的数据文件（holdings.csv、account.csv、operations.csv、logs/ 等）不会被覆盖。

set -u
PROJECT="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
CHECK=0; TEST_FEISHU=0; INSTALL_DEPS=0; NEW_OPEN_ID=""

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --test-feishu) TEST_FEISHU=1 ;;
    --install-deps) INSTALL_DEPS=1 ;;
    --open-id) NEW_OPEN_ID="${2:-}"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
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
step "1. 检查运行环境"
if ! command -v python3 >/dev/null 2>&1; then
  if [ $INSTALL_DEPS = 1 ] && command -v brew >/dev/null 2>&1; then brew install python; fi
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

if ! command -v lark-cli >/dev/null 2>&1 && [ $INSTALL_DEPS = 1 ]; then
  command -v npm >/dev/null 2>&1 || { command -v brew >/dev/null 2>&1 && brew install node; }
  command -v npm >/dev/null 2>&1 && npm install -g @larksuite/cli
fi
if command -v lark-cli >/dev/null 2>&1; then
  ok "lark-cli $(lark-cli --version 2>/dev/null | awk '{print $NF}')"
  if lark-cli config show >/dev/null 2>&1; then
    ok "lark-cli 已配置飞书应用"
  else
    warn "lark-cli 未配置飞书应用：运行 lark-cli config init（填 app-id / app-secret），机器人需开通 im:message 权限"
  fi
else
  warn "未找到 lark-cli，飞书提醒不可用：npm install -g @larksuite/cli（或加 --install-deps）"
fi

# ---------------------------------------------------------------- 2. 目录与数据文件
step "2. 目录与数据文件"
for d in logs cache profiles announcements; do mkdir -p "$d"; done
ok "logs/ cache/ profiles/ announcements/"

BOM=$'\xEF\xBB\xBF'
make_csv() {  # 文件不存在时写入带 BOM 的表头
  if [ -f "$1" ]; then ok "$1 已存在，保持不动"
  else printf '%s%s\n' "$BOM" "$2" > "$1"; warn "$1 不存在，已创建空表头，请填写：$2"; fi
}
make_csv holdings.csv   "名称,代码,市场,持仓数量,可用数量,成本价,币种,当前定位"
make_csv account.csv    "更新日期,总资产,总市值,可用资金,浮动盈亏,仓位"
make_csv operations.csv "日期,时间,操作,名称,代码,数量,价格,备注"

# ---------------------------------------------------------------- 3. 路径与飞书接收人
step "3. 修正项目路径与飞书接收人"
CRONS="$PROJECT/setup/crons.json"
if [ ! -f "$CRONS" ]; then
  fail "缺少 setup/crons.json（定时任务定义），无法生成定时任务指令"
else
  python3 - "$PROJECT" "$NEW_OPEN_ID" <<'PY'
import json, re, sys, pathlib
project, new_id = sys.argv[1], sys.argv[2]
root = pathlib.Path(project)
crons_p = root / "setup" / "crons.json"
data = json.loads(crons_p.read_text("utf-8"))
old_project, old_id = data["source_project"], data["feishu_open_id"]
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
if new_id and new_id != old_id:
    rules = rules.replace(old_id, new_id)
    for t in data["tasks"]:
        t["prompt"] = t["prompt"].replace(old_id, new_id)
    changed.append(f"飞书 open_id {old_id} → {new_id}")
    data["feishu_open_id"] = new_id
data["source_project"] = project
rules_p.write_text(rules, "utf-8")
crons_p.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
for c in changed:
    print(f"  \033[32m✓\033[0m 已替换{c}（intraday_rules.md、setup/crons.json）")
if not changed:
    print(f"  \033[32m✓\033[0m 路径与 open_id 无需修改（{project}，{data['feishu_open_id']}）")
PY
fi

# ---------------------------------------------------------------- 4. fate 命令
step "4. 安装 fate 命令"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/fate" <<EOF
#!/bin/bash
# fate wheel：刷新并在浏览器打开 fate_wheel 持仓看板（由 setup.sh 生成）
PROJECT="$PROJECT"
EOF
cat >> "$BIN_DIR/fate" <<'EOF'

usage() {
  cat <<USAGE
用法：
  fate wheel            用本地缓存重新生成看板并打开（不联网，约 1~3 秒；页面打开后自动拉实时行情）
  fate wheel -f         先联网重抓盘中快照 + 日 K / 技术位（限速下约 3~5 分钟）再生成并打开
  fate wheel -o         不重新生成，直接打开现有 dashboard.html
USAGE
}

case "$1" in
  wheel)
    cd "$PROJECT" || exit 1
    case "$2" in
      -o|--open) ;;
      -f|--fresh) python3 build_dashboard.py fresh=true || echo "生成失败，打开旧看板" ;;
      "") python3 build_dashboard.py || echo "生成失败，打开旧看板" ;;
      *) usage; exit 1 ;;
    esac
    open "$PROJECT/dashboard.html"
    ;;
  *) usage; exit 1 ;;
esac
EOF
chmod +x "$BIN_DIR/fate"
ok "$BIN_DIR/fate"
case ":$PATH:" in
  *":$BIN_DIR:"*) ok "$BIN_DIR 已在 PATH 中" ;;
  *)
    if grep -qs "$BIN_DIR" "$HOME/.zshenv"; then
      ok "~/.zshenv 已包含 ${BIN_DIR}，新开终端生效"
    else
      echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$HOME/.zshenv"
      ok "已把 $BIN_DIR 加入 ~/.zshenv，新开终端生效"
    fi ;;
esac

# ---------------------------------------------------------------- 5. 自检
step "5. 自检"
if N=$(python3 fetch_data.py list 2>/dev/null | grep -c '^|'); then
  ok "fetch_data.py 可运行（接口表 $N 行）"
else
  fail "python3 fetch_data.py list 运行失败"
fi
if [ $CHECK = 1 ]; then
  echo "  运行盘中 snapshot（约 10~30 秒）…"
  if python3 fetch_data.py snapshot mode=intraday > cache/setup_check.json 2>/dev/null; then
    ERR=$(python3 -c "import json;d=json.load(open('cache/setup_check.json'));print(len(d.get('errors',[])))" 2>/dev/null)
    ok "snapshot 成功，errors ${ERR:-?} 项（详见 cache/setup_check.json）"
  else
    fail "snapshot 失败，检查网络"
  fi
  python3 build_dashboard.py >/dev/null 2>&1 && ok "dashboard.html 已生成" || fail "build_dashboard.py 失败"
fi
if [ $TEST_FEISHU = 1 ] && command -v lark-cli >/dev/null 2>&1; then
  OID=$(python3 -c "import json;print(json.load(open('setup/crons.json'))['feishu_open_id'])")
  if lark-cli im +messages-send --as bot --user-id "$OID" \
      --markdown "fate_wheel 配置测试：$(hostname) 上的飞书提醒已打通。" >/dev/null 2>&1; then
    ok "飞书测试消息已发送给 $OID"
  else
    fail "飞书测试消息发送失败：检查 lark-cli config show / auth status，以及机器人是否对该用户可用"
  fi
fi

# ---------------------------------------------------------------- 6. 定时任务
step "6. 定时任务（需在 MiniMax Code 中创建）"
OUT="$PROJECT/setup/CRON_SETUP_PROMPT.md"
python3 - "$PROJECT" > "$OUT" <<'PY'
import json, sys
project = sys.argv[1]
d = json.load(open(f"{project}/setup/crons.json", encoding="utf-8"))
names = "、".join([t["cron_name"] for t in d["tasks"]] + d.get("legacy_cron_names", []))
print(f"""请为持仓盯盘项目重建定时任务，项目目录：{project}

1. 先用 cron_read(view=list) 查看现有任务；名称为下列之一的旧任务（{names}）先用 cron_delete 删除，避免重复运行。
2. 读取 {project}/setup/crons.json，对 tasks 里的每一条调用 cron_create，参数逐字使用文件里的值：
   cron_name、prompt、schedule、timezone、session、agent_name，并额外传 project="{project}"。prompt 不要改写或摘要。
3. 创建完用 cron_read(view=list) 核对：共 {len(d['tasks'])} 条、project 都是 {project}、enabled=true，列出各任务下次运行时间。

另外安装「休眠兜底」组件（重要）：MiniMax Code 的 cron 调度器跑在 GUI 进程里，机器合盖睡眠时到点的任务会被静默跳过且不补跑。请执行：

   cd {project} && ./setup_sleep_guard.sh install

它会加载两个 launchd agent：
   - com.fatewheel.collect   每个分析时点前 1 分钟采集行情快照到 cache/snapshots/（保证错过时段后数据还在）
   - com.fatewheel.catchup   每 10 分钟 + 每次唤醒检测缺失时段并补写 logs/yyyyMMdd.md

其中「设置定时唤醒」需要 sudo 密码。你没有密码时会跳过这一步，agent 照常装好；请提醒用户在本机终端自己执行：
   cd {project} && ./setup_sleep_guard.sh wake
（唤醒点覆盖未来 30 天，到期要重跑 wake 续期；合盖且没接外接显示器时唤醒可能失效，补跑才是真正的兜底。）

装完用 ./setup_sleep_guard.sh status 确认：两个 agent 已加载，并列出定时唤醒情况。""")
PY
ok "已生成 setup/CRON_SETUP_PROMPT.md"
command -v pbcopy >/dev/null 2>&1 && pbcopy < "$OUT" && ok "指令已复制到剪贴板"
echo "  → 在 MiniMax Code 里新建会话（工作目录选 ${PROJECT}），粘贴下面这段指令："
echo "  ------------------------------------------------------------"
sed 's/^/  /' "$OUT"
echo "  ------------------------------------------------------------"

step "7. 安装休眠兜底（定时采集 + 错过时段补跑）"
if launchctl list 2>/dev/null | grep -q "com.fatewheel.catchup"; then
  ok "已安装（com.fatewheel.collect / com.fatewheel.catchup）"
else
  warn "未安装。机器合盖睡眠时，MiniMax Code 的定时任务会被静默跳过且不补跑。"
  echo "  → 执行 ./setup_sleep_guard.sh install  安装（定时唤醒那步需要 sudo 输密码；之后每 30 天跑一次 ./setup_sleep_guard.sh wake 续期）"
  echo "  → 装完用 ./setup_sleep_guard.sh status  查看状态"
fi

step "完成"
if [ $WARNINGS = 0 ]; then ok "全部通过"; else warn "共 $WARNINGS 项需要处理（见上方 ! / ✗）"; fi
echo "  打开看板：fate wheel"
