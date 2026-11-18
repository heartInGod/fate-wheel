"""数据文件归档与统一路径解析（data multi-dir，简称 datamul）。

散落在项目根目录的数据文件按「内容性质」归档到 data/ 下的不同子目录：

  data/portfolio/   账户与持仓台账（个人资产，最敏感）
      account.csv, holdings.csv, operations.csv, thesis.json
  data/config/      人维护的规则与策略配置
      rules_params.json, risk_policy.json, policy_signals.json, core_sleeves.json
  data/derived/     脚本自动生成的校准 / 复查产物（可再生成）
      rules_auto.json, core_auto.json
  data/watchlist/   自选股名单
      watchlist.csv

所有代码通过 data_path(name) 访问这些文件，读写共用同一解析，因此归档后
业务逻辑无需改动。未在 ARCHIVE 登记的名字（cache/、logs/、profile 等）保持
原有语义：data_path("cache", "x") 等价于 os.path.join(ROOT, "cache", "x")。

注意：本模块只负责路径解析，不搬动文件。实际迁移由一次性 git mv 完成，
新目录里的文件就是运行时真正读写的目标；目录需预先存在（随 git mv 建立）。

命令行：
  python3 fw/datamul.py list    # 列出归档映射
  python3 fw/datamul.py check   # 检查每个登记文件是否已在归档位置、根目录是否还有残留
"""
import os
import sys

# 既可被 `import fw.datamul` 使用，也可直接 `python3 fw/datamul.py` 运行：
# 后者脚本目录是 fw/，需先把项目根放进 sys.path 才能解析 fw 包。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fw.paths import ROOT

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
    print(f"\n合计 {len(ARCHIVE)} 个登记文件，{problems} 个待处理。")
    return 1 if problems else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "list"
    if cmd == "check":
        return _cmd_check()
    if cmd == "list":
        _cmd_list()
        return 0
    print(f"未知命令: {cmd}\n用法: datamul.py [list|check]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
