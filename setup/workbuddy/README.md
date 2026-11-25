# WorkBuddy 定时任务定义（已弃用）

> ⚠️ **已迁移到 MiniMax Code**（`setup/minimax/`，2026-11-24）。本目录仅作历史留档，`setup.sh` 不再使用它。
> 如果 WorkBuddy 里还有名称以「【fate-wheel】」开头的 automation，**请全部删除**，否则会和 MCode 的
> 定时任务重复执行同一份分析。

 fate-wheel 的 9 条定时任务曾托管在 **WorkBuddy 原生定时任务（automation）**，不再依赖 MiniMax Code。

## 目录内容

| 文件 | 作用 |
|---|---|
| `automations.json` | 9 条任务的机器可读定义（name / rrule / status / prompt_file），由 `_generate.py` 生成 |
| `prompts/*.txt` | 每条任务的完整 prompt（含 WSL 环境头、时点判断、业务步骤） |
| `_generate.py` | 从 `../crons.json` 派生上面两份产物；改了调度或 prompt 后重跑它 |

## 为什么这样设计

1. **WorkBuddy 的 RRULE 限制**：BYHOUR / BYMINUTE 只接受单个整数，无法表达"每 10 分钟、覆盖多个小时"。
   所以原来盘中 25 个时点按"分钟偏移"合并成 6 条（`:00/:10/:20/:30/:40/:50`），每条每小时触发一次，
   靠 prompt 里的【时点判断】只在有效小时执行；15:00 收盘并入 `:00` 那条（intraday_rules.md 第 7 条）。
2. **调度器在 Windows、项目在 WSL**：每条 prompt 都要求 agent 用 `wsl.exe -e bash -lc` 执行项目命令。
3. **prompt 保真**：原始 prompt 在 `setup/crons.json`（MiniMax 时代文案），派生时自动完成三件事：
   加 WSL 环境头、删除 MiniMax 特有的会话归档/`task_output` 步骤、给盘中任务加时点判断。
   不要手改 `prompts/*.txt`，改 `crons.json` 后重跑 `_generate.py`。

## 重新安装 / 修改

- **重装**：跑 `./setup.sh`，它会生成 `setup/WORKBUDDY_SETUP_PROMPT.md`；在 WorkBuddy 新会话里粘贴，
  工具会删除所有「【fate-wheel】」前缀的旧任务并按 `automations.json` 重建（不影响其它任务）。
- **改调度或 prompt**：
  1. 编辑 `setup/crons.json`（改时间用 `schedule`，改步骤用 `prompt`）；
  2. `python3 setup/workbuddy/_generate.py` 重新派生；
  3. 在 WorkBuddy 里删除对应任务并按新的 `automations.json` 重建。

## 9 条任务一览

| 任务 | RRULE | 说明 |
|---|---|---|
| 盘前分析 09:00 | `FREQ=DAILY;BYHOUR=9;BYMINUTE=0` | 每天盘前，含周末（prompt 内判断休市） |
| 盘中 :00 | `FREQ=HOURLY;...;BYMINUTE=0` | 仅 10/11/13/14/15 点执行，15 点写收盘小结 |
| 盘中 :10/:20 | `FREQ=HOURLY;...;BYMINUTE=10/20` | 仅 10/11/13/14 点执行 |
| 盘中 :30 | `FREQ=HOURLY;...;BYMINUTE=30` | 仅 9/10/11/13/14 点执行 |
| 盘中 :40/:50 | `FREQ=HOURLY;...;BYMINUTE=40/50` | 仅 9/10/13/14 点执行（11 点不分析） |
| 收盘评估 16:00 | `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=16;BYMINUTE=0` | evaluate + calibrate + 写日志 + 刷看板 |
| 周报 周五 17:00 | `FREQ=WEEKLY;BYDAY=FR;BYHOUR=17;BYMINUTE=0` | 长线复查 + 政策周报 |

时区：WorkBuddy RRULE 用系统本地时间（Asia/Shanghai），无需额外设置。

## 已知限制

- **通知**：统一走 `python3 main.py notify`，渠道在 `config/notify.json`（默认 `wechat`，走 Windows 侧微信个人号桥接，
  见 `setup/wechat_bridge/README.md`）。桥接未启动时任务会照常分析、记录、写日志，只在日志里注明
  「通知未发送（原因）」。切飞书：`./setup.sh --channel feishu` + `lark-cli config init`。
- **睡眠漏跑**：WorkBuddy 也是 GUI 调度器，机器合盖深睡时到点任务会被跳过。macOS 上可用
  `setup/setup_sleep_guard.sh` 装 launchd 兜底（采集快照 + 补写日志）；其它平台发现日志缺失时手动
  `python3 main.py catchup`、`python3 main.py snapshot` 补跑。
