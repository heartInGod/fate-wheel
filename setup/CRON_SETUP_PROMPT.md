请为持仓盯盘项目重建定时任务，项目目录：/home/fox/projects/fate-wheel

1. 先用 cron_read(view=list) 查看现有任务；名称为下列之一的旧任务（每日9点持仓分析与飞书提醒、盘中10分钟持仓分析（9:30-9:50）、盘中10分钟持仓分析（10/13/14点整段）、盘中10分钟持仓分析（11:00-11:30）、盘中持仓分析（15:00收盘）、收盘建议评估（16:00）、每周五长线仓复查与飞书周报、盘中15分钟持仓分析（9:30-9:45）、盘中15分钟持仓分析（10/13/14点整段）、盘中15分钟持仓分析（11:00-11:30）、收盘建议评估（15:15））先用 cron_delete 删除，避免重复运行。
2. 读取 /home/fox/projects/fate-wheel/setup/crons.json，对 tasks 里的每一条调用 cron_create，参数逐字使用文件里的值：
   cron_name、prompt、schedule、timezone、session、agent_name，并额外传 project="/home/fox/projects/fate-wheel"。prompt 不要改写或摘要。
3. 创建完用 cron_read(view=list) 核对：共 7 条、project 都是 /home/fox/projects/fate-wheel、enabled=true，列出各任务下次运行时间。

当前平台为 wsl：休眠兜底脚本 setup/setup_sleep_guard.sh 依赖 macOS 的 launchd / pmset，在此平台不可用，已跳过。
MiniMax Code 的 cron 调度器在机器睡眠时到点的任务仍可能被静默跳过；若发现日志缺失，可手动补跑：

   cd /home/fox/projects/fate-wheel && python3 main.py catchup --dry-run    # 只看缺哪些时段
   cd /home/fox/projects/fate-wheel && python3 main.py catchup              # 补写 logs/yyyyMMdd.md
   cd /home/fox/projects/fate-wheel && python3 main.py snapshot             # 补采当前时点行情快照

建议在系统电源设置里避免机器在分析时段睡眠，以减少漏跑。
