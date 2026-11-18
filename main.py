#!/usr/bin/env python3
"""fate-wheel 统一入口。

所有功能以子命令形式调用，项目根目录下只需这一个脚本：

  python3 main.py <命令> [原脚本的参数...]

例如（等价于重构前的 `python3 xxx.py ...`）：

  python3 main.py fetch snapshot mode=intraday       数据快照
  python3 main.py signals --no-record                预览候选信号
  python3 main.py core review                        长线仓复查
  python3 main.py etf --help                         ETF 估值
  python3 main.py calibrate show                     查看当前阈值
  python3 main.py evaluate --date 20261110           指定日复盘
  python3 main.py thesis list                        查看持仓前提
  python3 main.py dashboard                          刷新看板
  python3 main.py catchup --dry-run                  补齐漏跑时段

  python3 main.py list            列出全部命令
  python3 main.py test            运行全部单元测试

子命令通过 runpy 以 __main__ 方式加载对应脚本，脚本原有入口逻辑零改动；
数据路径由 common/paths.py 统一提供，与脚本所在目录无关。

根目录只保留本入口脚本；其余模块按职责位于 common/ core/ engine/ agent/ datasources/。
数据文件归档校验：python3 main.py datamul check
"""
import os
import runpy
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
# paths.py 已收进 common/，先让它可被找到；import 时它会自动 setup_sys_path()
sys.path.insert(0, os.path.join(ROOT, "common"))
sys.path.insert(0, ROOT)

from paths import setup_sys_path

setup_sys_path()

# 子命令 -> (相对项目根的脚本路径, 说明)
COMMANDS = {
    # 核心计算 / 估值 / 风控
    "signals":     ("core/signals_engine.py",  "盘中规则引擎：算候选信号"),
    "core":        ("core/core_value.py",      "长线价值仓：目标价与复查"),
    "etf":         ("core/etf_value.py",       "ETF 指数估值法"),
    "industry":    ("core/industry_heat.py",   "行业景气分"),
    "policy":      ("core/policy_signal.py",   "政策信号分"),
    "risk-core":   ("core/risk_core.py",       "风险核心"),
    "risk-exp":    ("core/risk_experiment.py", "风险实验"),
    "portfolio":   ("core/portfolio_risk.py",  "组合风险（影子模式）"),
    # 评估 / 校准 / 记录
    "calibrate":   ("engine/calibrate.py",     "动态阈值校准"),
    "evaluate":    ("engine/evaluate.py",      "收盘建议评估"),
    "record":      ("engine/record_signal.py", "信号采纳/否决/发送记录"),
    "record-op":   ("engine/record_op.py",     "实际成交记录"),
    # 数据获取 / 归档工具
    "fetch":       ("datasources/fetch_data.py", "统一数据获取入口"),
    "datamul":     ("common/datamul.py",        "数据归档路径解析与校验"),
    # 持仓前提
    "thesis":      ("core/thesis_cli.py",       "维护持仓定位前提"),
    # Agent 定时任务辅助
    "catchup":     ("agent/catchup.py",        "补齐错过的分析时段"),
    "snapshot":    ("agent/collect_snapshot.py", "计划时点行情快照归档"),
    "premarket":   ("agent/premarket_brief.py", "盘前简报"),
    "dashboard":   ("agent/build_dashboard.py", "生成看板 dashboard.html"),
}


def show_help():
    print(__doc__)
    print("可用命令：")
    width = max(len(k) for k in COMMANDS)
    for name, (_, desc) in COMMANDS.items():
        print(f"  {name:<{width}}  {desc}")
    print("  " + "-" * (width + 2 + len("说明")))
    print(f"  {'list':<{width}}  列出全部命令")
    print(f"  {'test':<{width}}  运行全部单元测试")


def run_tests():
    import glob
    import unittest

    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    test_dir = os.path.join(ROOT, "tests")
    sys.path.insert(0, test_dir)
    for path in sorted(glob.glob(os.path.join(test_dir, "test_*.py"))):
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            suite.addTests(loader.loadTestsFromName(name))
        except Exception as exc:  # 收集阶段出错也计入失败
            print(f"加载 {name} 失败: {exc}", file=sys.stderr)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        show_help()
        return 0

    cmd = argv[0]
    if cmd == "list":
        show_help()
        return 0
    if cmd == "test":
        return run_tests()
    if cmd not in COMMANDS:
        print(f"未知命令: {cmd}\n", file=sys.stderr)
        show_help()
        return 2

    rel_path, _ = COMMANDS[cmd]
    target = os.path.join(ROOT, rel_path)
    if not os.path.exists(target):
        print(f"脚本不存在: {rel_path}", file=sys.stderr)
        return 2

    # 把参数原样交给目标脚本：sys.argv[0] 设为目标脚本，其余为子命令参数
    sys.argv = [target] + argv[1:]
    runpy.run_path(target, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
