# AGENTS.md · fate-wheel

给 AI Agent 的项目约定。业务规则见 `intraday_rules.md`，数据接口见 `DATA_SOURCES.md`，
定时任务见 `setup/minimax/README.md`。本文件只放「怎么做」的硬约定。

## 运行环境

- 项目代码与数据在 WSL：`/home/fox/projects/fate-wheel`；Windows 侧映射 `W:\home\fox\projects\fate-wheel`。
- 所有 `python3 main.py ...` 命令经 WSL 执行：`wsl.exe -e bash -lc 'cd /home/fox/projects/fate-wheel && <命令>'`。
  `wsl.exe` 开头的 `Failed to translate ...` 是噪音，不影响结果。
- 文件读写用相对工作目录的**相对路径**（`data/report/x.md`）；`W:\...` 绝对路径会被工具拒绝。

## 通知

- 统一走 `python3 main.py notify`，渠道在 `config/notify.json`（当前 `feishu`，lark-cli 机器人私聊）。
- **只发总结正文，不发文件**：不要把 `data/report/*.md` 或任何报告作为附件发出；直接用 `lark-cli im +messages-send --file` 发文件同样禁止。
  注意区分：`python3 main.py notify --file cache/notify_msg.txt` 里的 `--file` 是**从文件读消息正文**，不是发附件，可以正常用。
  报告写进项目文件，通知里只留结论、关键数字和操作建议。
- 发送失败不反复重试，在当天日志注明「通知未发送（原因）」，其余任务照常完成。

## 数据与文件约束

- 日志 `data/report/yyyyMMdd.md` **只追加**，不改已有内容。
- 禁止修改：`data/portfolio/holdings.csv`、`account.csv`、`operations.csv`、`thesis.json`（只能经 `thesis.py` 写）、
  `rules_params.json`、`data/config/*`、`intraday_rules.md`、`rules_auto.json`（由 calibrate.py 生成）、所有 `.py` 及 `datasources/`。
- 缺口指标不臆造：接口不可用时写明「接口不可用」，不要填猜测值。
