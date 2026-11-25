#!/usr/bin/env python3
"""从 setup/crons.json 派生 MiniMax Code（MCode）定时任务定义（setup/minimax/）。

生成物：
  setup/minimax/tasks.json          7 条主力定时任务的机器可读定义（cron_name / schedule / prompt_file / model）
  setup/minimax/prompts/*.txt       每条主力任务适配后的完整 prompt
  setup/minimax/tasks_fallback.json 7 条兜底定时任务定义（绑 claude-opus-5-5，错峰触发）
  setup/minimax/fallback/*.txt      每条兜底任务的 prompt

兜底设计（MCode 没有任务级模型 failover，gpt-6.2-astra 503 时会话在调模型阶段就报错，
根本读不到 prompt，所以兜底必须是一个独立的、绑别的模型的会话）：
  - 每档主力任务配一条镜像任务，跑在 claude-opus-5-5 上，只比主力晚 2 分钟触发。
  - 兜底任务只做一件事：先判断这一档是否已经由主力完成。
    日志里已有本档段落          → 主力成了 → 立即结束，不写日志、不通知、不跑分析。
    会话 status 还是 `started`  → 主力还在跑 → 也立即结束（不加这一步，健康日会每档重复分析）。
    status 是 `error` / 没这个会话 → 主力死了 → 自己完整执行对应 prompts 文件补上这一档。
  - 10 分钟网格那两档每档间隔只有 10 分钟，兜底固定在 +2 分钟，先把「当前时间减 2 分钟再向下取整到
    10 分钟」算成主力时点，再用该时点的段落标题判断，避免调度延迟导致误判。

适配要点（WorkBuddy → MiniMax Code）：
  1. MCode 的 cron 支持标准 5 段表达式（含列表/范围），直接沿用 crons.json 的原始 schedule，
     不再需要 WorkBuddy 那种"每小时触发 + prompt 内时点判断"的绕法。
  2. 加统一「运行环境」头：MCode 跑在 Windows（PowerShell），项目在 WSL，
     命令一律经 wsl.exe 执行；文件可用 W:\\ 映射路径直接读写。
  3. 收盘评估第 5b 步（会话归档）在 MCode 下有效，保留，
     但把 workspaceDir / purpose 的筛选条件改成 MCode 下的实际值。

MCode 中的定时任务只放一句"读取 prompts/xxx.txt 并执行"，改 prompt 只需改 crons.json 后重跑本脚本，
无需重建定时任务；改调度（schedule）或模型才需要在 MCode 里更新任务。

用法：python3 setup/minimax/_generate.py
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "setup", "crons.json")
OUT_DIR = os.path.join(ROOT, "setup", "minimax")
PROMPT_DIR = os.path.join(OUT_DIR, "prompts")
FALLBACK_DIR = os.path.join(OUT_DIR, "fallback")
WSL = "/home/fox/projects/fate-wheel"
WIN = r"W:\home\fox\projects\fate-wheel"
MODEL = "custom_provider:mafia/gpt-6.2-astra"  # GPT 6.2 Astra（主力）
MODEL_FALLBACK = "custom_provider:mafia-anthropic/claude-opus-5-5"  # Claude Opus 5.5（兜底）

# 主力 schedule 从 crons.json 读出来填进来，main() 里初始化
SCHEDULES = {}

ENV_HEAD = """【运行环境 · 必读】
你现在由 MiniMax Code 定时任务触发执行。
  - 你的工作目录：{win}（Windows 路径）；项目代码与数据在 WSL：{wsl}（同一份文件）。
  - **文件读写**：用 read / edit / write 工具，路径一律写**相对工作目录的相对路径**，例如
    `setup/minimax/prompts/x.txt`、`data/report/20261124.md`、`cache/notify_msg.txt`。
    不要写 `{win}\\...` 这种绝对路径（工具会拒绝：UNC/映射盘路径被拦），也不要写 `/home/...`（Windows 侧不认）。
  - **命令**：所有 `python3 main.py ...`、`date` 等一律通过 WSL 执行：
      wsl.exe -e bash -lc 'cd {wsl} && <命令>'
    例：wsl.exe -e bash -lc 'cd {wsl} && python3 main.py signals'
    wsl.exe 会先打印几行 "Failed to translate ..." 路径警告，是噪音，不影响结果与退出码。
    不要在 Windows 侧直接运行 python。
  - 日期以 WSL 的 `date +%Y%m%d` 为准（Asia/Shanghai）。
  - 联网搜索用 web_search 工具（freshness=day/week）。
  - 运行时间长的命令（如 calibrate、core review、snapshot）若被转入后台，用 task_output 等它跑完再继续。
  - 通知统一用 `python3 main.py notify`，渠道在 config/notify.json，当前是 **feishu（飞书机器人私聊）**。
    PowerShell → wsl → bash 多层引号容易吃掉消息里的引号：正文先用 write 工具写到 `cache/notify_msg.txt`，
    再运行 wsl.exe -e bash -lc 'cd {wsl} && python3 main.py notify --file cache/notify_msg.txt'。
    发送失败不要反复重试，在当天日志注明「通知未发送（原因）」，任务其余部分照常完成。不要自己拼 lark-cli 命令。
  - **通知只发总结正文，不要发文件**：不要把 data/report/*.md 等报告文件作为附件/文件发出（飞书、微信都一样），
    也不要用 lark-cli 的 --file。报告照常写进项目文件，手机上只看正文里的结论、数字和操作建议。
  - 下文出现的相对路径（data/、data/report/、logs/、cache/ 等）都相对项目根目录；
    下文的「工作目录：{wsl}」即上面的项目路径。

""".format(wsl=WSL, win=WIN)

# 会话归档（收盘评估第 5b 步）：MCode 会话的 workspaceDir 是 Windows 侧路径
ARCHIVE_OLD = "workspaceDir 为 /home/fox/projects/fate-wheel"
ARCHIVE_NEW = "workspaceDir 为 {win}（或 {wsl}）".format(win=WIN, wsl=WSL)
PURPOSE_OLD = "purpose 以 `cron:main:` 开头"
PURPOSE_NEW = "purpose 以 `cron:` 开头（定时任务创建的会话）"

# 主力任务 + 各自的兜底配置。字段：
#   src         crons.json 里的 cron_name
#   file        适配后的 prompt 文件名
#   name        MCode 主力任务名
#   model       主力模型
#   fb_name     兜底任务名
#   fb_schedule 兜底 cron（在主力时点之后错峰，避免和主力同时抢跑）
#   fb_check    写进兜底 prompt 的「本档是否已完成」判断规则
#   fb_slot     通知里用来指代这一档的短标签
TASKS = [
    {
        "src": "每日9点持仓分析与通知提醒",
        "file": "premarket_0900.txt",
        "name": "【fate-wheel】盘前分析 09:00",
        "fb_name": "【fate-wheel·兜底】盘前分析 09:02",
        "fb_schedule": "2 9 * * *",
        "fb_check": "本档段落标题固定为 `## 09:00 盘前分析`。日志里存在这个标题（含「定时复核」等后缀变体，"
                    "只要行首是 `## 09:00 盘前分析` 就算）即为已完成。",
        "fb_slot": "09:00 盘前分析",
    },
    {
        "src": "盘中分析（9:45）",
        "file": "intraday_0945.txt",
        "name": "【fate-wheel】盘中分析 9:45",
        "fb_name": "【fate-wheel·兜底】盘中分析 9:47",
        "fb_schedule": "47 9 * * 1-5",
        "fb_check": "本档段落标题固定为 `## 09:45 盘中分析`。日志里存在该标题即为已完成。",
        "fb_slot": "09:45 盘中分析",
    },
    {
        "src": "盘中10分钟持仓分析（10/13/14点整段）",
        "file": "intraday_10_13_14.txt",
        "name": "【fate-wheel】盘中分析 10/13/14 点每 10 分钟",
        "fb_name": "【fate-wheel·兜底】盘中 10/13/14 点（错峰 2 分钟）",
        "fb_schedule": "2,12,22,32,42,52 10,13,14 * * 1-5",
        "fb_check": "主力是 10 分钟一档（0,10,20,30,40,50 分）。先跑\n"
                    "     wsl.exe -e bash -lc 'date -d -2min +%H:%M'\n"
                    "   把结果再**向下取整到 10 分钟**，得到的 `HH:MM` 就是本档主力时点，"
                    "本档段落标题是 `## HH:MM 盘中分析`。例：现在 10:02 → 算得 10:00 → 搜 `## 10:00 盘中分析`；"
                    "现在 13:42 → 算得 13:40 → 搜 `## 13:40 盘中分析`。",
        "fb_slot": "本次盘中",
    },
    {
        "src": "盘中10分钟持仓分析（11:00-11:30）",
        "file": "intraday_11.txt",
        "name": "【fate-wheel】盘中分析 11:00-11:30",
        "fb_name": "【fate-wheel·兜底】盘中 11:00-11:30（错峰 2 分钟）",
        "fb_schedule": "2,12,22,32 11 * * 1-5",
        "fb_check": "主力是 11:00/11:10/11:20/11:30 四档。先跑\n"
                    "     wsl.exe -e bash -lc 'date -d -2min +%H:%M'\n"
                    "   把结果再**向下取整到 10 分钟**，得到的 `HH:MM` 就是本档主力时点，"
                    "本档段落标题是 `## HH:MM 盘中分析`。例：现在 11:12 → 算得 11:10 → 搜 `## 11:10 盘中分析`；"
                    "现在 11:32 → 算得 11:30 → 搜 `## 11:30 盘中分析`。",
        "fb_slot": "本次盘中",
    },
    {
        "src": "盘中持仓分析（15:00收盘）",
        "file": "close_1500.txt",
        "name": "【fate-wheel】收盘分析 15:00",
        "fb_name": "【fate-wheel·兜底】收盘分析 15:02",
        "fb_schedule": "2 15 * * 1-5",
        "fb_check": "本档段落标题固定为 `## 15:00 收盘分析`。日志里存在该标题即为已完成"
                    "（收盘小结是它的子标题 `### 收盘小结`，不单独判断）。",
        "fb_slot": "15:00 收盘分析",
    },
    {
        "src": "收盘建议评估（16:00）",
        "file": "evaluate_1600.txt",
        "name": "【fate-wheel】收盘建议评估 16:00",
        "fb_name": "【fate-wheel·兜底】收盘建议评估 16:02",
        "fb_schedule": "2 16 * * 1-5",
        "fb_check": "本档段落标题固定为 `## 16:00 建议评估`。日志里存在该标题即为已完成。",
        "fb_slot": "16:00 建议评估",
    },
    {
        "src": "每周五长线仓复查与通知周报",
        "file": "weekly_fri_1700.txt",
        "name": "【fate-wheel】长线仓复查周报 周五 17:00",
        "fb_name": "【fate-wheel·兜底】长线仓复查周报 周五 17:02",
        "fb_schedule": "2 17 * * 5",
        "fb_check": "本档段落标题固定为 `## 周度长线复查`。日志里存在该标题即为已完成。",
        "fb_slot": "周五周报",
    },
]

# 兜底 prompt 正文模板。占位符见 fallback_prompt()。
FB_BODY = """【兜底任务 · 必读】
你是「{primary}」的兜底副本，你自己跑在 Claude Opus 5.5 上。
主力任务绑的模型是 {model_primary}，它一旦请求失败就整档作废——
失败时会话在调用模型阶段就报错退出，日志、通知、报告一个都不会写。
所以你的职责只有一句话：**主力没做成，你立刻接手；主力做成了或还在做，你马上走人。**

（主力周期 `{src_schedule}`，本兜底周期 `{fb_schedule}`，只比主力晚 2 分钟触发。）

【第一步：先判断，不要一上来就干活】
1. 取今天的日期（只跑这一条 shell；**不要加内层引号**，多层 shell 会把引号吃掉）：
     wsl.exe -e bash -lc 'cd {wsl} && date +%Y%m%d'
   结果记作 YYYYMMDD。
2. 本档要检查的段落标题：
   {check}
3. 用 **grep 工具**在 `data/report/YYYYMMDD.md` 里搜该标题
   （用工具，不要用 shell 的 grep，也不要拼管道）。
   - **搜到了** → 主力这一档已经写成 → 立即结束：不写日志、不发通知、不跑任何分析。
   - 文件不存在 / 搜不到 → 继续第 4 步。
4. 确认主力这一档是「死了」还是「还在跑」——**这步别省**，
   省了会在 Astra 健康的日子里把每一档都重复分析一遍：
   用 mavis 工具跑 `mavis session list`，找 purpose 为 `{purpose}`、
   createdAt 落在本档时点前后 90 秒内的那个会话。
   - status 是 `error` → 主力失败 → 进入第二步补跑。
   - status 是 `started` → 主力还在跑 → 本次**直接结束**，什么都不要做。
   - 找不到这类会话 → 调度没投递成功 → 按失败处理，进入第二步补跑。
   - mavis 工具不可用或查不出结果 → 也按失败处理，进入第二步补跑。

【第二步：补跑】
用 read 工具完整读取 `setup/minimax/prompts/{fn}`（相对工作目录的路径），
严格逐条执行其中**全部**指令：步骤一个不减，数据口径一个不改。
有两处与本档补跑相关：
  - 写日志前再 grep 一次；如果本档段落已经出现（主力刚好写完了），立刻停手，不要重复追加。
  - 通知：**没有新信号就不发**，跟主力一样，不要为了「告知兜底已接管」单独发一条。
    如果本档确实产生了需要发给张小凡的新信号，正文第一行加一句：
    `【兜底】主力 gpt-6.2-astra 未完成「{slot}」这一档，本次由 Opus 5.5 补跑。`

【结束条件】补跑完成、或判定主力已成功/仍在跑，本次任务即结束。
不要顺手补别的历史时段，不要做与这一档无关的任何事。"""


def fallback_prompt(t, fb_schedule):
    return ENV_HEAD + FB_BODY.format(
        primary=t["name"],
        model_primary=MODEL,
        src_schedule=SCHEDULES[t["src"]],
        fb_schedule=fb_schedule,
        wsl=WSL,
        check=t["fb_check"],
        purpose="cron:mavis:" + t["name"],
        fn=t["file"],
        slot=t["fb_slot"],
    )


def adapt(prompt: str) -> str:
    if ARCHIVE_OLD in prompt:
        prompt = prompt.replace(ARCHIVE_OLD, ARCHIVE_NEW).replace(PURPOSE_OLD, PURPOSE_NEW)
    return ENV_HEAD + prompt


def main():
    src = json.load(open(SRC, encoding="utf-8"))
    by_name = {t["cron_name"]: t for t in src["tasks"]}
    for t in TASKS:
        SCHEDULES[t["src"]] = by_name[t["src"]]["schedule"]
    os.makedirs(PROMPT_DIR, exist_ok=True)
    os.makedirs(FALLBACK_DIR, exist_ok=True)

    out, fb_out = [], []
    for t in TASKS:
        s = by_name[t["src"]]
        fn, name = t["file"], t["name"]
        with open(os.path.join(PROMPT_DIR, fn), "w", encoding="utf-8", newline="\n") as f:
            f.write(adapt(s["prompt"]))
        out.append({
            "cron_name": name,
            "source_cron_name": t["src"],
            "schedule": s["schedule"],
            "timezone": s.get("timezone", "Asia/Shanghai"),
            "session": {"mode": "new"},
            "model": MODEL,
            "project": WIN,
            "prompt_file": "prompts/" + fn,
            "prompt": (
                "请先用 read 工具完整读取 `{pf}`（相对工作目录的路径），"
                "然后严格逐条执行其中全部指令，不得省略。该文件已包含运行环境说明和任务正文。"
            ).format(pf="setup/minimax/prompts/" + fn),
        })

        fb_fn = fn.replace(".txt", ".fallback.txt")
        with open(os.path.join(FALLBACK_DIR, fb_fn), "w", encoding="utf-8", newline="\n") as f:
            f.write(fallback_prompt(t, t["fb_schedule"]))
        fb_out.append({
            "cron_name": t["fb_name"],
            "guards_cron_name": name,
            "schedule": t["fb_schedule"],
            "primary_schedule": s["schedule"],
            "timezone": s.get("timezone", "Asia/Shanghai"),
            "session": {"mode": "new"},
            "model": MODEL_FALLBACK,
            "project": WIN,
            "prompt_file": "fallback/" + fb_fn,
            "prompt": (
                "请先用 read 工具完整读取 `{pf}`（相对工作目录的路径），"
                "然后严格逐条执行其中全部指令，不得省略。该文件已包含运行环境说明、"
                "兜底判断规则和任务正文。"
            ).format(pf="setup/minimax/fallback/" + fb_fn),
        })

    meta = {
        "_说明": "MiniMax Code 定时任务定义，由 setup/minimax/_generate.py 从 setup/crons.json 派生。"
                 "MCode 里每条任务的 prompt 只是读取 prompts/ 下对应文件并执行。",
        "wsl_project": WSL,
        "win_project": WIN,
        "task_count": len(out),
        "tasks": out,
    }
    path = os.path.join(OUT_DIR, "tasks.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write("\n")

    fb_meta = {
        "_说明": "MCode 没有任务级模型 failover。这 7 条兜底任务绑 " + MODEL_FALLBACK +
                 "，在主力时点之后错峰触发，靠 data/report/yyyyMMdd.md 里本档段落是否已存在来判断"
                 "主力是否跑成：已存在就立即结束，不存在才补跑对应的 prompts 文件。",
        "primary_model": MODEL,
        "fallback_model": MODEL_FALLBACK,
        "win_project": WIN,
        "task_count": len(fb_out),
        "tasks": fb_out,
    }
    fb_path = os.path.join(OUT_DIR, "tasks_fallback.json")
    with open(fb_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(fb_meta, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"已生成 {len(out)} 条主力任务 -> {path}")
    print(f"已生成 {len(fb_out)} 条兜底任务 -> {fb_path}")
    for a in out:
        print(f"  [主力] {a['cron_name']:<38} {a['schedule']}")
    for a in fb_out:
        print(f"  [兜底] {a['cron_name']:<38} {a['schedule']}")


if __name__ == "__main__":
    main()

