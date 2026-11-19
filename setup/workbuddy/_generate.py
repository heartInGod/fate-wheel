#!/usr/bin/env python3
"""一次性生成 WorkBuddy 定时任务定义（setup/workbuddy/），从 setup/crons.json 派生。

生成物：
  setup/workbuddy/automations.json   10 条 automation 的机器可读定义（盘前1 + 盘中7 + 评估1 + 周报1）
  setup/workbuddy/prompts/*.txt      每条任务适配后的完整 prompt

适配要点（MiniMax Code → WorkBuddy）：
  1. 加统一「运行环境」头：项目路径、WSL 命令前缀、WebSearch 替代 web_search、
     无 mavis/task_output、通知降级说明。
  2. 删除任务5的第 5b 步（mavis 会话归档）及正文里对它的引用。
  3. 盘中任务加「时点判断」头：本条只负责特定分钟偏移的有效小时，
     其余时刻直接结束（WorkBuddy 最小调度粒度是每小时，无法表达"每10分钟"）。

用法：python3 setup/workbuddy/_generate.py
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "setup", "crons.json")
OUT_DIR = os.path.join(ROOT, "setup", "workbuddy")
PROMPT_DIR = os.path.join(OUT_DIR, "prompts")
WSL = "/home/fox/projects/fate-wheel"

ENV_HEAD = """【运行环境 · 必读】
你现在由 WorkBuddy 定时任务触发执行（不是 MiniMax Code）。工作目录与所有数据都在 WSL 里：
  - WSL 项目路径：{wsl}
  - 所有 `python3 main.py ...`、`date`、文件读写命令，一律通过 WSL 执行：
      wsl.exe -e bash -lc 'cd {wsl} && <命令>'
    例：wsl.exe -e bash -lc 'cd {wsl} && python3 main.py signals'
  - 联网搜索用 WebSearch 工具（原文里的 `web_search` 即指它；freshness=day/week）。
  - 原 prompt 里的 `mavis` / `session` / `task_output` 是 MiniMax Code 专有工具，WorkBuddy 没有：
      · "task_output 等结果" → 直接等待命令跑完即可（命令是同步执行的）。
      · 会话归档相关步骤已删除，无需处理。
  - 通知（微信/飞书）：统一用 `python3 main.py notify --text '...'`（长消息用 --file 或 --stdin），
    渠道在 config/notify.json 的 channel 配置（默认 wechat）。不要自己拼 lark-cli 命令。
    若发送失败，不要反复重试，在当天日志注明「通知未发送（原因）」即可，任务其余部分照常完成。
  - 下文出现的相对路径（data/、data/report/、logs/、cache/ 等）都相对 {wsl}。

""".format(wsl=WSL)

# 盘中时点合并：按分钟偏移分组，每条 automation 每小时的第 N 分钟触发，
# prompt 内判断当前小时是否在有效集合内（有效小时来自 crons.json 的原 schedule）。
INTRADAY_GROUPS = [
    # (name, minute, 有效小时列表, 说明)
    # 2026-11-19 调整：9 点只保留 9:45 一次（原 9:30/9:40/9:50 取消）。
    # 15:00 收盘保留（并入 :00 组，按 intraday_rules.md 第 7 条写收盘小结）。
    ("盘中分析 · 9点45分", 45, [9], ""),
    ("盘中分析 · 每整点(10/11/13/14/15点)", 0,  [10, 11, 13, 14, 15],
     "小时为 15 时按规则文件第 7 条写收盘小结；其余小时按普通盘中分析。"),
    ("盘中分析 · 每小时10分(10/11/13/14点)", 10, [10, 11, 13, 14], ""),
    ("盘中分析 · 每小时20分(10/11/13/14点)", 20, [10, 11, 13, 14], ""),
    ("盘中分析 · 每小时30分(10/11/13/14点)", 30, [10, 11, 13, 14], ""),
    ("盘中分析 · 每小时40分(10/13/14点)", 40, [10, 13, 14], ""),
    ("盘中分析 · 每小时50分(10/13/14点)", 50, [10, 13, 14], ""),
]

# 11 点的 4 个时点（00/10/20/30）已被上面的组覆盖。
SPECIAL_INTRADAY = [
    # 11 点整段: 0,10,20,30 —— 用 HOURLY 会混入其它小时，改为按分钟并入上面的组时需注意
]

SLOT_GUARD = """【时点判断 · 先做这一步】
本任务在每小时的第 {minute} 分钟被触发，但只负责以下小时：{hours}。
请先执行 wsl.exe -e bash -lc 'date +%H' 取得当前小时（24 小时制，返回如 10）：
  - 若当前小时不在 {{ {hours_list} }} 内，或今天是周末/节假日休市，立即结束本次任务，
    不写日志、不发消息、不运行任何脚本。
  - 若在有效小时内，继续下面的正文。{desc}
注意：WorkBuddy 的最小调度粒度是"每小时"，所以本任务每小时都会触发一次，
靠这里的时间判断还原原来"每 10 分钟、仅交易时段"的节奏，不要去掉这段判断。

"""


def strip_minimax_session(text: str) -> str:
    """删除任务5第5b步会话归档，并清理正文里对它的引用。"""
    # 删除第 5b 步整段（从 "5b. 归档今天的分析会话：" 到下一个编号 "6. "）
    start = text.find("5b. 归档今天的分析会话：")
    if start != -1:
        end = text.find("\n6. ", start)
        if end != -1:
            text = text[:start] + text[end + 1:]
    # 第 1 步里对 5b 的引用
    text = text.replace("只执行第 5b 步归档会话（不发消息、不写日志），然后结束",
                        "不发消息、不写日志，然后结束")
    text = text.replace("和第 5b 步，然后结束", "，然后结束")
    # 第 6 步约束里对 5b 的引用
    text = text.replace("、按第 5b 步归档本项目定时任务会话", "")
    # 第 1b 步里对 task_output 的引用（WorkBuddy 无此工具，命令是同步执行的）
    text = text.replace("（动态阈值校准，约 1.5 分钟，等它跑完再继续；若被转入后台，用 task_output 等结果）",
                        "（动态阈值校准，约 1.5 分钟，同步等待它跑完再继续）")
    return text


def main():
    src = json.load(open(SRC, encoding="utf-8"))
    tasks = src["tasks"]
    os.makedirs(PROMPT_DIR, exist_ok=True)

    automations = []

    # ---- 1. 盘前 09:00（每天）----
    p = tasks[0]["prompt"]
    fn = "premarket.txt"
    open(os.path.join(PROMPT_DIR, fn), "w", encoding="utf-8").write(ENV_HEAD + p)
    automations.append({
        "name": "【fate-wheel】盘前分析 09:00",
        "prompt_file": "prompts/" + fn,
        "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        "status": "ACTIVE",
        "description": "原每日9点持仓分析与通知提醒；每天 09:00",
    })

    # ---- 2. 盘中 7 条（按分钟偏移）----
    # 取原盘中任务的正文（4 条内容基本一致，用第 2 条"10/13/14 点整段"做基底，
    # 它覆盖的时点最多；9:45 与 11 点段的差异由时点判断头 + 正文里"读 intraday_rules.md"覆盖）
    base_intraday = tasks[2]["prompt"]
    base_intraday = base_intraday.replace("（这是 10/13/14 点整段那几次）", "")
    close_hint = (
        "\n本时点若当前小时为 15（收盘），按 intraday_rules.md 第 7 条"
        "在日志追加收盘小结；建议对错由 16:00 评估任务打分，这里不自评。\n"
    )
    for name, minute, hours, desc in INTRADAY_GROUPS:
        guard = SLOT_GUARD.format(
            minute=minute,
            hours="/".join(str(h) for h in hours),
            hours_list=",".join(str(h) for h in hours),
            desc=desc,
        )
        extra = close_hint if minute == 0 else ""
        fn = f"intraday_{minute:02d}.txt"
        open(os.path.join(PROMPT_DIR, fn), "w", encoding="utf-8").write(
            ENV_HEAD + guard + extra + base_intraday)
        automations.append({
            "name": f"【fate-wheel】{name}",
            "prompt_file": "prompts/" + fn,
            "rrule": f"FREQ=HOURLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYMINUTE={minute}",
            "status": "ACTIVE",
            "description": f"盘中分析，每小时 :{minute:02d} 触发，仅 {hours} 点执行"
                           + (f"；{desc}" if desc else ""),
        })

    # ---- 3. 11 点整段的 4 个时点 ----
    # 11:00/10/20/30 已被上面的 :00/:10/:20/:30 组覆盖（它们在有效小时里含 11），
    # 无需额外任务；但 11:40/11:50 原本不分析——上面的 :40/:50 组有效小时不含 11，正确。

    # ---- 5. 收盘评估 16:00（工作日）----
    p = strip_minimax_session(tasks[5]["prompt"])
    fn = "evaluate_1600.txt"
    open(os.path.join(PROMPT_DIR, fn), "w", encoding="utf-8").write(ENV_HEAD + p)
    automations.append({
        "name": "【fate-wheel】收盘建议评估 16:00",
        "prompt_file": "prompts/" + fn,
        "rrule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=16;BYMINUTE=0",
        "status": "ACTIVE",
        "description": "原收盘建议评估；工作日 16:00。已删除 MiniMax 会话归档步骤",
    })

    # ---- 6. 周五长线复查 17:00 ----
    p = tasks[6]["prompt"]
    fn = "weekly_1700.txt"
    open(os.path.join(PROMPT_DIR, fn), "w", encoding="utf-8").write(ENV_HEAD + p)
    automations.append({
        "name": "【fate-wheel】长线仓复查周报 周五17:00",
        "prompt_file": "prompts/" + fn,
        "rrule": "FREQ=WEEKLY;BYDAY=FR;BYHOUR=17;BYMINUTE=0",
        "status": "ACTIVE",
        "description": "原每周五长线仓复查与通知周报；每周五 17:00",
    })

    meta = {
        "_说明": "WorkBuddy 定时任务定义，由 setup/workbuddy/_generate.py 从 setup/crons.json 派生。"
                 "setup.sh 安装时读取本文件，调用 WorkBuddy 的定时任务工具逐条创建/更新。"
                 "prompt 全文在 prompts/ 目录，创建时读入。",
        "source_project": src["source_project"],
        "wsl_project": WSL,
        "automation_count": len(automations),
        "timezone": "Asia/Shanghai (系统本地时间，RRULE 不带时区字段)",
        "automations": automations,
    }
    out = os.path.join(OUT_DIR, "automations.json")
    json.dump(meta, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"已生成 {len(automations)} 条 automation 定义 -> {out}")
    for a in automations:
        print(f"  - {a['name']:<42} {a['rrule']}")


if __name__ == "__main__":
    main()
