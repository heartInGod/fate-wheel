"""数据文件归档与统一路径解析（data multi-dir，简称 datamul）。

散落在项目根目录的数据文件按「内容性质」归档到 data/ 下的不同子目录：

  data/portfolio/   账户与持仓台账（个人资产，最敏感）
      account.csv, holdings.csv, operations.csv, thesis.json
  data/config/      人维护的规则与策略配置
      rules_params.json, risk_policy.json, policy_signals.json, core_sleeves.json
  data/derived/     脚本自动生成的校准 / 复查产物（可再生成）
      rules_auto.json, core_auto.json, dashboard.json, dashboard.js
  data/watchlist/   自选股名单
      watchlist.csv
  data/announcements/  公告档案（脚本增量维护，gitignore）；一只一个 {代码}.md
  data/research/    研究数据：社保选股结果（优选/补充）、policy/ 政策报告与原文存档

所有代码通过 data_path(文件名) / data_dir(目录名, ...) 访问这些文件，读写共用同一解析，
因此归档后业务逻辑无需改动。未在 ARCHIVE 登记的名字（cache/、logs/、profiles 等）保持
原有语义：data_path("cache", "x") 等价于 os.path.join(ROOT, "cache", "x")。

注意：本模块只负责路径解析，不搬动文件。实际迁移由一次性 git mv 完成，
新目录里的文件就是运行时真正读写的目标；目录需预先存在（随 git mv 建立）。

命令行：
  python3 main.py datamul list    # 列出归档映射
  python3 main.py datamul check   # 检查每个登记文件是否已在归档位置、根目录是否还有残留
"""
import os
import sys

# 既可被 `import datamul` 使用，也可直接 `python3 common/datamul.py` 运行：
# datamul.py 位于 <ROOT>/common/ 下，上溯两级即项目根目录。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from paths import ROOT

# 文件名 -> 归档子目录（相对 ROOT）。新增数据文件时在这里登记即可。
ARCHIVE = {
    # 账户与持仓台账
    "account.csv": "portfolio",
    "holdings.csv": "portfolio",
    "operations.csv": "portfolio",
    "thesis.json": "portfolio",
    # 人维护的配置
    "rules_params.json": "config",
    "risk_policy.json": "config",
    "policy_signals.json": "config",
    "core_sleeves.json": "config",
    # 脚本生成的产物
    "rules_auto.json": "derived",
    "core_auto.json": "derived",
    # 自选股
    "watchlist.csv": "watchlist",
}


def data_path(name, *parts):
    """返回数据文件的实际路径。

    已登记的文件名路由到 data/<子目录>/name；未登记的按原样相对 ROOT 解析，
    保证与 os.path.join(ROOT, ...) 行为一致。
    """
    sub = ARCHIVE.get(name)
    if sub:
        return os.path.join(ROOT, "data", sub, name, *parts)
    return os.path.join(ROOT, name, *parts)


# 目录级归档：整个目录（含其下的通配文件）从 ROOT 挪到 data/ 下。
# 与 ARCHIVE 的区别：这里是目录名 -> data 下子目录，调用方传目录名 + 相对片段。
DIR_ARCHIVE = {
    "announcements": "announcements",
    "research": "research",
}

# 目录级归档中「实际搬走的子项」：用于 check 校验目标存在、根目录无残留。
# announcements 整目录搬走（根目录下该目录应消失）；research 只搬数据，*.py 脚本留在研究目录（代码），
# 因此逐个校验搬走的子项，而不是要求整个 research/ 消失。
DIR_MOVED = {
    "announcements": ["."],
    "research": [
        "policy",
        "社保优选30_2026Q3.csv", "社保优选30_2026Q3.md",
        "社保补充_2026Q3.csv", "社保补充_2026Q3.json", "社保补充_2026Q3.md",
    ],
}


def data_dir(name, *parts):
    """返回归档目录（或其下文件）的路径。

    已登记的目录名路由到 data/<子目录>/<parts...>；未登记的按原名相对 ROOT 解析。
    例如 data_dir("announcements", "002558.md") -> ROOT/data/announcements/002558.md；
         data_dir("research", "policy", "PROMPT.md") -> ROOT/data/research/policy/PROMPT.md。
    """
    sub = DIR_ARCHIVE.get(name)
    if sub:
        return os.path.join(ROOT, "data", sub, *parts)
    return os.path.join(ROOT, name, *parts)


def archive_root():
    return os.path.join(ROOT, "data")


def _cmd_list():
    by = {}
    for name, sub in ARCHIVE.items():
        by.setdefault(sub, []).append(name)
    for sub in ("portfolio", "config", "derived", "watchlist"):
        print(f"data/{sub}/")
        for name in sorted(by.get(sub, [])):
            print(f"    {name:<24} -> {data_path(name)}")
    print("目录级归档：")
    for name, sub in DIR_ARCHIVE.items():
        print(f"    {name}/ -> data/{sub}/   例如 {data_dir(name, '示例.md')}")


def _cmd_check():
    problems = 0
    for name, sub in ARCHIVE.items():
        target = data_path(name)
        stray = os.path.join(ROOT, name)
        ok = os.path.exists(target) and not os.path.exists(stray)
        print(f"[{'OK' if ok else '!!'}] {name:<24} {target}")
        if not ok:
            problems += 1
            if os.path.exists(stray):
                print(f"        根目录仍有残留: {stray}")
            if not os.path.exists(target):
                print(f"        归档目标缺失: {target}")
    # 目录级归档：逐个校验「搬走的子项」目标存在、源位置无残留。
    # 例：research/ 下 *.py 脚本是代码，留在原处，不能因为根目录还有 research/ 就判失败。
    for name, sub in DIR_ARCHIVE.items():
        moved = DIR_MOVED.get(name, ["."])
        target_dir = data_dir(name)
        stray_root = os.path.join(ROOT, name)
        bad = []
        for item in moved:
            t = target_dir if item == "." else os.path.join(target_dir, item)
            s = stray_root if item == "." else os.path.join(stray_root, item)
            if not os.path.exists(t):
                bad.append(f"归档目标缺失: {t}")
            if os.path.exists(s):
                bad.append(f"根目录仍有残留: {s}")
        cnt = len(os.listdir(target_dir)) if os.path.isdir(target_dir) else 0
        print(f"[{'OK' if not bad else '!!'}] {name + '/':<24} {target_dir}（{cnt} 个文件）")
        for msg in bad:
            print(f"        {msg}")
        problems += len(bad)
    total = len(ARCHIVE) + len(DIR_ARCHIVE)
    print(f"\n合计 {total} 项（{len(ARCHIVE)} 文件 + {len(DIR_ARCHIVE)} 目录），{problems} 个待处理。")
    return 1 if problems else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "list"
    if cmd == "check":
        return _cmd_check()
    if cmd == "list":
        _cmd_list()
        return 0
    print(f"未知命令: {cmd}\n用法: main.py datamul [list|check]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
