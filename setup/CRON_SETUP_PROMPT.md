请为持仓盯盘项目重建定时任务，项目目录：/Users/fox/modelProject/fate_wheel

1. 先用 cron_read(view=list) 查看现有任务；名称为下列之一的旧任务（每日8点持仓分析与飞书提醒、盘中10分钟持仓分析（9:30-9:50）、盘中10分钟持仓分析（10/13/14点整段）、盘中10分钟持仓分析（11:00-11:30）、盘中持仓分析（15:00收盘）、收盘建议评估（15:15）、收盘建议评估（16:00）、每周五长线仓复查与飞书周报、盘中15分钟持仓分析（9:30-9:45）、盘中15分钟持仓分析（10/13/14点整段）、盘中15分钟持仓分析（11:00-11:30））先用 cron_delete 删除，避免重复运行。
2. 读取 /Users/fox/modelProject/fate_wheel/setup/crons.json，对 tasks 里的每一条调用 cron_create，参数逐字使用文件里的值：
   cron_name、prompt、schedule、timezone、session、agent_name，并额外传 project="/Users/fox/modelProject/fate_wheel"。prompt 不要改写或摘要。
3. 创建完用 cron_read(view=list) 核对：共 6 条、project 都是 /Users/fox/modelProject/fate_wheel、enabled=true，列出各任务下次运行时间。

另外安装「休眠兜底」组件（重要）：MiniMax Code 的 cron 调度器跑在 GUI 进程里，机器合盖睡眠时到点的任务会被静默跳过且不补跑。请执行：

   cd /Users/fox/modelProject/fate_wheel && ./setup_sleep_guard.sh install

它会加载两个 launchd agent：
   - com.fatewheel.collect   每个分析时点前 1 分钟采集行情快照到 cache/snapshots/（保证错过时段后数据还在）
   - com.fatewheel.catchup   每 10 分钟 + 每次唤醒检测缺失时段并补写 logs/yyyyMMdd.md

其中「设置定时唤醒」需要 sudo 密码。你没有密码时会跳过这一步，agent 照常装好；请提醒用户在本机终端自己执行：
   cd /Users/fox/modelProject/fate_wheel && ./setup_sleep_guard.sh wake
（唤醒点覆盖未来 30 天，到期要重跑 wake 续期；合盖且没接外接显示器时唤醒可能失效，补跑才是真正的兜底。）

装完用 ./setup_sleep_guard.sh status 确认：两个 agent 已加载，并列出定时唤醒情况。
