#!/usr/bin/env python3
"""经验文档路径解析与 30 天滚动清理。

目录结构（data/experience/，整体不入库）：
  current.md       活状态：经验条目 / 标的笔记 / 执行偏差，长期保留、不滚动
  YYYY-MM-DD.md    每日复盘，一天一个文件，滚动保留 30 天

用法：
  python3 main.py experience path       # 打印今天的复盘文件路径
  python3 main.py experience current    # 打印活状态文件路径
  python3 main.py experience list       # 列出所有复盘文件（按日期升序）
  python3 main.py experience cleanup    # 删除 30 天前的复盘文件（真删）
  python3 main.py experience cleanup --dry-run   # 只预览会删哪些，不真删

环境变量 FW_EXPERIENCE_KEEP_DAYS 可覆盖保留天数（默认 30）。
注意：cleanup 默认真实删除，验证时务必带 --dry-run，不要用极小 keep 值对真实目录测试。
"""
import os
import re
import sys
from datetime import date, timedelta

from paths import ROOT

EXP_DIR = os.path.join(ROOT, "data", "experience")
CURRENT = os.path.join(EXP_DIR, "current.md")
KEEP_DAYS = int(os.environ.get("FW_EXPERIENCE_KEEP_DAYS", "30"))
_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def ensure_dir():
    os.makedirs(EXP_DIR, exist_ok=True)


def current_path():
    return CURRENT


def daily_path(d=None):
    """今天（或指定日期）的复盘文件路径。"""
    if d is None:
        d = date.today()
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return os.path.join(EXP_DIR, d.isoformat() + ".md")


def list_daily():
    """返回 [(日期字符串, 路径), ...]，按日期升序。current.md 不计入。"""
    out = []
    if not os.path.isdir(EXP_DIR):
        return out
    for fn in os.listdir(EXP_DIR):
        m = _DAY.match(fn)
        if not m:
            continue
        try:
            date.fromisoformat(m.group(1))
        except ValueError:
            continue
        out.append((m.group(1), os.path.join(EXP_DIR, fn)))
    return sorted(out)


def cleanup(keep=KEEP_DAYS, today=None):
    """删除早于「今天往前 keep-1 天」的每日复盘文件，返回被删日期列表。"""
    today = today or date.today()
    if isinstance(today, str):
        today = date.fromisoformat(today[:10])
    cutoff = (today - timedelta(days=keep - 1)).isoformat()
    removed = []
    for day, path in list_daily():
        if day < cutoff:
            try:
                os.remove(path)
                removed.append(day)
            except OSError as e:
                print(f"[experience] 清理 {path} 失败: {e}", file=sys.stderr)
    return removed


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "list"
    if cmd == "path":
        print(daily_path())
    elif cmd == "current":
        print(CURRENT)
    elif cmd == "list":
        for day, path in list_daily():
            print(day, path)
    elif cmd == "cleanup":
        keep = KEEP_DAYS
        dry = "--dry-run" in argv
        for a in argv[1:]:
            if a.isdigit():
                keep = int(a)
        if dry:
            today = date.today()
            cutoff = (today - timedelta(days=keep - 1)).isoformat()
            would = [d for d, _ in list_daily() if d < cutoff]
            tail = "：" + ", ".join(would) if would else ""
            print(f"[dry-run] 保留最近 {keep} 天；将删除 {len(would)} 个复盘文件{tail}")
        else:
            removed = cleanup(keep=keep)
            tail = "：" + ", ".join(removed) if removed else ""
            print(f"保留最近 {keep} 天；已清理 {len(removed)} 个复盘文件{tail}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
