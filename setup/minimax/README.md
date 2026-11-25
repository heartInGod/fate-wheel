# MiniMax Code 定时任务定义

fate-wheel 的 7 条定时任务托管在 **MiniMax Code 原生定时任务（cron）**，不再依赖 WorkBuddy。

## 目录内容

| 文件 | 作用 |
|---|---|
| `tasks.json` | 7 条**主力**任务的机器可读定义（cron_name / schedule / timezone / model / prompt_file），由 `_generate.py` 生成 |
| `prompts/*.txt` | 每条主力任务的完整 prompt（含 Windows+WSL 环境头、任务正文、业务约束） |
| `tasks_fallback.json` | 7 条**兜底**任务定义（绑 Claude Opus 5.5、错峰触发），由 `_generate.py` 生成 |
| `fallback/*.txt` | 每条兜底任务的 prompt（判断规则 + 补跑指令） |
| `_generate.py` | 从 `../crons.json` 派生上面所有产物；改了 prompt 后重跑它 |

## 为什么这样设计

1. **prompt 与调度解耦**：MCode 里每条任务的 prompt 只有一句「读取 `setup/minimax/prompts/xxx.txt` 并严格执行」。
   改业务步骤只改 `setup/crons.json` → 重跑 `_generate.py`，**不需要重建定时任务**，下次运行自动生效。
   只有改调度或模型才需要在 MCode 里更新任务。
2. **cron 表达式用回原始写法**：MCode 支持标准 5 段表达式（含列表），
   所以 `setup/crons.json` 里 `0,10,20,30,40,50 10,13,14 * * 1-5` 这类原样可用。
   WorkBuddy 时代的「每小时触发 + prompt 内时点判断」绕法在 MCode 下不需要了。
3. **调度器在 Windows、项目在 WSL**：每条 prompt 都要求 agent 用 `wsl.exe -e bash -lc` 执行项目命令；
   读写文件也可以直接用 `W:\home\fox\projects\fate-wheel\...` 映射路径。
   `wsl.exe` 会先打印 "Failed to translate" 路径警告，是噪音，不影响结果与退出码。
4. **模型：主力 + 兜底两层**。7 条主力任务统一用 GPT 6.2 Astra（`custom_provider:mafia/gpt-6.2-astra`），
   另有 7 条镜像兜底任务绑 Claude Opus 5.5（`custom_provider:mafia-anthropic/claude-opus-5-5`）。
   详见下一节。
5. **通知**：统一走 `python3 main.py notify`，渠道在 `config/notify.json`，**当前是 feishu**（lark-cli 机器人私聊，
   2026-11-24 切换；微信桥接自 11-20 起不可用）。
   因为要穿过 PowerShell → wsl → bash 多层引号，prompt 里统一要求「先用 write 把消息写到 `cache/notify_msg.txt`，
   再用 `notify --file`」，避免引号被吃掉（`--file` 是读**消息正文**，不是发附件）。
6. **只发正文，不发附件**：通知里不要附 `data/report/*.md` 等报告文件，也不要用 lark-cli 的 `--file` 发文件。
   报告照常写进项目文件，手机上只看总结里的结论、数字和操作建议。

## 现在有哪些任务

| 任务名 | cron | 说明 |
|---|---|---|
| 【fate-wheel】盘前分析 09:00 | `0 9 * * *` | 每天，含周末（prompt 内判断休市） |
| 【fate-wheel】盘中分析 9:45 | `45 9 * * 1-5` | 9:45 那一次 |
| 【fate-wheel】盘中分析 10/13/14 点每 10 分钟 | `0,10,20,30,40,50 10,13,14 * * 1-5` | 每 10 分钟 |
| 【fate-wheel】盘中分析 11:00-11:30 | `0,10,20,30 11 * * 1-5` | 11 点整段（无 :40/:50） |
| 【fate-wheel】收盘分析 15:00 | `0 15 * * 1-5` | 按 intraday_rules.md 第 7 条写收盘小结 |
| 【fate-wheel】收盘建议评估 16:00 | `0 16 * * 1-5` | evaluate + calibrate + 经验更新 + 会话归档 |
| 【fate-wheel】长线仓复查周报 周五 17:00 | `0 17 * * 5` | core review + 政策周报 |

时区统一 `Asia/Shanghai`。

## 兜底机制（gpt-6.2-astra 失败时改走 Claude Opus 5.5）

**为什么不能只改一个配置**：MCode 的定时任务没有模型级 failover（`config.yaml` 里没有 fallback 字段，
`cron_update` 也只有单一 `model`）。更关键的是，Astra 上游 503 时会话**在调用模型阶段就报错退出**，
连 prompt 都没读到，所以「在同一个任务里写 if 失败则换模型」是做不到的——兜底必须是一个**独立的、绑别的模型的会话**。

**做法**：每档主力配一条镜像任务，跑在 Claude Opus 5.5 上，**只比主力晚 2 分钟**触发。
镜像任务只做两件事：

1. **判断**：先用 grep 工具在 `data/report/YYYYMMDD.md` 里搜本档的段落标题（如 `## 10:00 盘中分析`）。
   搜到了 = 主力已经写成了 → 立即结束，不写日志、不发通知、不跑任何分析。
   搜不到，再确认主力这一档是「死了」还是「还在跑」（用 `mavis session list` 看该档会话的 status）：
   `error` 或压根没有会话 → 死了，进入第 2 步；还是 `started` → 还在跑，同样立即结束。
   **这一步不能省**：健康的主力头两分钟也不会写日志，只看日志会把每一档都重复分析一遍。
2. **补跑**：完整执行对应的 `prompts/xxx.txt`。无新信号照常不发通知（与主力一致）；
   真有信号时通知正文首行加「【兜底】……」。

| 主力任务 | 主力 cron | 兜底 cron | 错峰 | 判断用的段落标题 |
|---|---|---|---|---|
| 盘前分析 09:00 | `0 9 * * *` | `2 9 * * *` | +2 分 | `## 09:00 盘前分析` |
| 盘中分析 9:45 | `45 9 * * 1-5` | `47 9 * * 1-5` | +2 分 | `## 09:45 盘中分析` |
| 盘中 10/13/14 每 10 分钟 | `0,10,20,30,40,50 10,13,14 * * 1-5` | `2,12,22,32,42,52 10,13,14 * * 1-5` | +2 分 | `## HH:MM 盘中分析` |
| 盘中 11:00-11:30 | `0,10,20,30 11 * * 1-5` | `2,12,22,32 11 * * 1-5` | +2 分 | `## HH:MM 盘中分析` |
| 收盘分析 15:00 | `0 15 * * 1-5` | `2 15 * * 1-5` | +2 分 | `## 15:00 收盘分析` |
| 收盘建议评估 16:00 | `0 16 * * 1-5` | `2 16 * * 1-5` | +2 分 | `## 16:00 建议评估` |
| 长线仓复查周报 周五 17:00 | `0 17 * * 5` | `2 17 * * 5` | +2 分 | `## 周度长线复查` |

两点实现细节：

- **两条 10 分钟网格必须小于 10 分钟**：间隔只有 10 分钟，兜底不能跨到下一档。
  因为可能被调度延迟，兜底先跑 `date -d -2min +%H:%M` 再向下取整到 10 分钟算出主力时点，
  用那个时点的标题判断，不会误判成「没跑」。
- **兜底 prompt 里只用 `date -d -2min +%H:%M` 这种不带内层引号的命令**：
  PowerShell → wsl → bash 多层传递会吃掉引号（实测 `date -d "-7 minutes"` 会变成
  `date: invalid date '-7'`）。读日志一律用 grep 工具，不要拼 shell 管道。

**已知残余风险**：主力如果在 2 分钟之后才死（例如跑到第 5 分钟才 503），兜底那时已经看到
`started` 并退出了，这一档就会丢。代价是丢一档，不影响下一档；换成「等到主力跑完再判断」又会把
延迟拉回到 7~9 分钟，所以按「宁可偶尔丢一档，也不要每档晚 7 分钟」取舍。

**代价**：兜底任务在主力正常时每次只花两次工具调用（grep 日志 + 查一次会话 status）就退出，
不重复分析。只有主力真挂了它才会完整补跑。全部 14 条任务可在 `cron_read` 里看到，
主力绑 Astra、兜底绑 Opus 5.5。

## 修改流程

**只改业务步骤（prompt）**
1. 编辑 `setup/crons.json` 的 `prompt` 字段；
2. `python3 setup/minimax/_generate.py`；
3. 完事——MCode 里的任务下次运行就按新 prompt 执行。

**改调度时间**
1. 改 `setup/crons.json` 的 `schedule` 并重跑 `_generate.py`；
2. 在 MCode 里更新对应主力任务的 schedule，**并同步更新它的兜底任务**（日程要维持错峰关系）。

**新增 / 删除任务**
在 `_generate.py` 的 `TASKS` 列表里增删一项（每项自带 `fb_name` / `fb_schedule` / `fb_check`），
重跑后再在 MCode 里建/删**主力 + 兜底两条**任务。

## 依赖

- WSL 里已装 python3（3.9+，只用标准库）。
- `config/notify.json` 已配置为 feishu：依赖 WSL 里的 `lark-cli`（`npm install -g @larksuite/cli --prefix ~/.local`）+
  应用 `cli_a9278f6fc4785cbd` 凭据（`~/.lark-cli/config.json`）+ 接收人 open_id。
  切回微信：把 `channel` 改回 `wechat` 并启动 Windows 侧桥接，见 `setup/wechat_bridge/README.md`。
  通知失败时任务照常分析、记录、写日志，只在日志里注明「通知未发送（原因）」。
- 机器睡眠时 MCode 到点任务可能被跳过；发现日志缺失时手动补跑 `python3 main.py catchup`、`python3 main.py snapshot`。
