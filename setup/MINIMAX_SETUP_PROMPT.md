请在 MiniMax Code 里为持仓盯盘项目重建定时任务（cron）。

## 背景
项目原先把定时任务托管在 WorkBuddy，现改为 MiniMax Code 原生定时任务。
任务定义与逐条 prompt 已在项目内生成好，不要自己重新设计调度；fate-wheel 之外的任务一律不要动。

## 要创建的任务（共 7 条，全部 enabled）
  - 【fate-wheel】盘前分析 09:00：`0 9 * * *`（Asia/Shanghai，project=W:\home\fox\projects\fate-wheel，model=custom_provider:mafia/gpt-6.2-astra）
  - 【fate-wheel】盘中分析 9:45：`45 9 * * 1-5`（Asia/Shanghai，project=W:\home\fox\projects\fate-wheel，model=custom_provider:mafia/gpt-6.2-astra）
  - 【fate-wheel】盘中分析 10/13/14 点每 10 分钟：`0,10,20,30,40,50 10,13,14 * * 1-5`（Asia/Shanghai，project=W:\home\fox\projects\fate-wheel，model=custom_provider:mafia/gpt-6.2-astra）
  - 【fate-wheel】盘中分析 11:00-11:30：`0,10,20,30 11 * * 1-5`（Asia/Shanghai，project=W:\home\fox\projects\fate-wheel，model=custom_provider:mafia/gpt-6.2-astra）
  - 【fate-wheel】收盘分析 15:00：`0 15 * * 1-5`（Asia/Shanghai，project=W:\home\fox\projects\fate-wheel，model=custom_provider:mafia/gpt-6.2-astra）
  - 【fate-wheel】收盘建议评估 16:00：`0 16 * * 1-5`（Asia/Shanghai，project=W:\home\fox\projects\fate-wheel，model=custom_provider:mafia/gpt-6.2-astra）
  - 【fate-wheel】长线仓复查周报 周五 17:00：`0 17 * * 5`（Asia/Shanghai，project=W:\home\fox\projects\fate-wheel，model=custom_provider:mafia/gpt-6.2-astra）

说明：MCode 的 cron 支持标准 5 段表达式（含逗号列表），所以调度直接用回原始写法，
不需要 WorkBuddy 时代「每小时触发 + prompt 内判断小时」的绕法。
每条任务的 prompt 只是一句「读取 setup/minimax/prompts/xxx.txt 并严格执行」，
业务步骤都在那个文件里——以后改 prompt 只要重跑 setup/minimax/_generate.py，不必重建任务。

## 执行步骤
1. 先用 cron_read（view=list）查看现有任务；cronName 以「【fate-wheel】」开头的旧任务全部删除，
   避免重复运行。注意只删 fate-wheel 前缀的，不要动其它任务。
2. 读取 `W:/home/fox/projects/fate-wheel/setup/minimax/tasks.json`，对 tasks 里的每一条调用 cron_create：
   - cron_name 取 `cron_name`，schedule 取 `schedule`，timezone 取 `timezone`；
   - model 取 `model`；project 取 `project`（Windows 路径）；
   - session 取 `{"mode": "new"}`；agent_name 用 `me`；
   - prompt 直接用文件里的 `prompt` 字段（它已指向该条的 `prompt_file`）。
3. 创建完核对：共 7 条、都是 enabled，并列出各条 nextRun。

## 环境说明
- 项目代码与数据在 WSL：`/home/fox/projects/fate-wheel`；MCode 调度器在 Windows 侧，
  所以每条 prompt 都要求 agent 用 `wsl.exe -e bash -lc` 执行项目命令。
- 通知统一走 `python3 main.py notify`，渠道在 config/notify.json（默认 wechat，走 Windows 侧微信个人号桥接）。
  详见 setup/wechat_bridge/README.md。
- 时区：任务按 `timezone` 字段（Asia/Shanghai）触发。
