请在 WorkBuddy 里为持仓盯盘项目重建定时任务。

## 背景
项目原先把定时任务托管在 MiniMax Code，现 MiniMax Code 已停用，改为 WorkBuddy 原生定时任务（automation）。
任务定义与逐条 prompt 已在项目内生成好，不要自己重新设计调度；fate-wheel 之外的任务一律不要动。

## 要创建的任务（共 9 条，全部 ACTIVE）
  - 【fate-wheel】盘前分析 09:00: `FREQ=DAILY;BYHOUR=9;BYMINUTE=0`
  - 【fate-wheel】盘中分析 · 每整点(10/11/13/14/15点): `FREQ=HOURLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYMINUTE=0`
  - 【fate-wheel】盘中分析 · 每小时10分(10/11/13/14点): `FREQ=HOURLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYMINUTE=10`
  - 【fate-wheel】盘中分析 · 每小时20分(10/11/13/14点): `FREQ=HOURLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYMINUTE=20`
  - 【fate-wheel】盘中分析 · 每小时30分(9/10/11/13/14点): `FREQ=HOURLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYMINUTE=30`
  - 【fate-wheel】盘中分析 · 每小时40分(9/10/13/14点): `FREQ=HOURLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYMINUTE=40`
  - 【fate-wheel】盘中分析 · 每小时50分(9/10/13/14点): `FREQ=HOURLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYMINUTE=50`
  - 【fate-wheel】收盘建议评估 16:00: `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=16;BYMINUTE=0`
  - 【fate-wheel】长线仓复查周报 周五17:00: `FREQ=WEEKLY;BYDAY=FR;BYHOUR=17;BYMINUTE=0`

说明：WorkBuddy 的 RRULE 不支持一条规则里写多个时点（BYHOUR/BYMINUTE 只能是单个整数），
所以原来"每 10 分钟、覆盖多个小时"的盘中任务，按"分钟偏移"合并成了 6 条（:00/:10/:20/:30/:40/:50），
每条每小时触发一次，由 prompt 里的【时点判断】还原原来只在交易时段、每 10 分钟分析的节奏。
15:00 收盘并入 :00 那一条（按 intraday_rules.md 第 7 条写收盘小结）。

## 执行步骤
1. 先用 automation 列表工具查看现有任务；名称以「【fate-wheel】」开头的旧任务全部删除，避免重复运行。
   注意只删 fate-wheel 前缀的，不要动其它任务。
2. 读取 `W:/home/fox/projects/fate-wheel/setup/workbuddy/automations.json`，对 automations 里的每一条：
   - 用 automation 创建工具（recurring）创建，参数取文件里的 `name`、`rrule`、`status`；
   - `prompt` 由你现场生成，模板固定为下面这段（把 {文件} 换成该条的 `prompt_file`）：

     请先用 Read 工具完整读取 `W:/home/fox/projects/fate-wheel/setup/workbuddy/prompts/{文件}`，
     然后严格逐条执行其中全部指令，不得省略。该文件已包含运行环境说明和任务正文。

   - 工作目录设为 `W:/home/fox/projects/fate-wheel`（Windows 路径）。
3. 创建完核对：共 9 条、都是 ACTIVE，并列出各条下次运行时间。

## 环境说明
- 项目代码与数据在 WSL：`/home/fox/projects/fate-wheel`；WorkBuddy 调度器在 Windows 侧，
  所以每条 prompt 都要求 agent 用 `wsl.exe -e bash -lc` 执行项目命令。
- 通知统一走 `python3 main.py notify`，渠道在 config/notify.json 配置（默认 wechat，走 Windows 侧微信个人号桥接）。
  桥接未启动时任务会在日志注明并跳过通知，不影响分析与记录；详见 setup/wechat_bridge/README.md。
- 时区：WorkBuddy RRULE 用系统本地时间（Asia/Shanghai），无需额外设置。
