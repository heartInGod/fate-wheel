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
数据路径由 src/common/paths.py 统一提供，与脚本所在目录无关。

根目录只保留本入口脚本；其余模块按职责位于 src/{common,core,engine,agent,datasources,research}/，
测试位于 src/tests/，前端资源位于 web/。
数据文件归档校验：python3 main.py datamul check
经验文档（data/experience/）：python3 main.py experience list|current|path|cleanup
"""
import os
import runpy
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
# paths.py 已收进 src/common/，先让它可被找到；import 时它会自动 setup_sys_path()
sys.path.insert(0, os.path.join(SRC, "common"))
sys.path.insert(0, SRC)
sys.path.insert(0, ROOT)

from paths import setup_sys_path

setup_sys_path()

# 子命令 -> (相对项目根的脚本路径, 说明)
COMMANDS = {
    # 核心计算 / 估值 / 风控
    "signals":     ("src/core/signals_engine.py",  "盘中规则引擎：算候选信号"),
    "core":        ("src/core/core_value.py",      "长线价值仓：目标价与复查"),
    "etf":         ("src/core/etf_value.py",       "ETF 指数估值法"),
    "industry":    ("src/core/industry_heat.py",   "行业景气分"),
    "policy":      ("src/core/policy_signal.py",   "政策信号分"),
    "risk-core":   ("src/core/risk_core.py",       "风险核心"),
    "risk-exp":    ("src/core/risk_experiment.py", "风险实验"),
    "portfolio":   ("src/core/portfolio_risk.py",  "组合风险（影子模式）"),
    # 评估 / 校准 / 记录
    "calibrate":   ("src/engine/calibrate.py",     "动态阈值校准"),
    "evaluate":    ("src/engine/evaluate.py",      "收盘建议评估"),
    "record":      ("src/engine/record_signal.py", "信号采纳/否决/发送记录"),
    "record-op":   ("src/engine/record_op.py",     "实际成交记录"),
    # 数据获取 / 归档工具
    "fetch":       ("src/datasources/fetch_data.py", "统一数据获取入口"),
    "datamul":     ("src/common/datamul.py",        "数据归档路径解析与校验"),
    "experience":  ("src/common/experience_store.py", "经验文档路径与30天滚动清理"),
    # 持仓前提
    "thesis":      ("src/core/thesis_cli.py",       "维护持仓定位前提"),
    # Agent 定时任务辅助
    "catchup":     ("src/agent/catchup.py",        "补齐错过的分析时段"),
    "snapshot":    ("src/agent/collect_snapshot.py", "计划时点行情快照归档"),
    "premarket":   ("src/agent/premarket_brief.py", "盘前简报"),
    "notify":      ("src/agent/notify.py",           "统一通知出口（微信/飞书，配置可切换）"),
    "dashboard":   ("src/agent/build_dashboard.py", "生成看板数据 dashboard.json / dashboard.js"),
}


def show_logs(args):
    """查询系统运行日志 logs/YYYY-MM-DD/ops.log（技术侧：耗时/阶段/异常）。"""
    import glob
    import logroll
    days = 90
    tail = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "path":
            print(logroll.latest("ops.log"))
            return 0
        if a == "days" and i + 1 < len(args):
            days = int(args[i + 1]); i += 1
        elif a == "tail" and i + 1 < len(args):
            tail = int(args[i + 1]); i += 1
        elif a.isdigit():
            tail = int(a)
        i += 1

    files = [f for f in glob.glob(os.path.join(logroll.SYS_LOGS, "*", "ops.log"))
             if os.path.isfile(f)]
    files.sort()
    files = files[-days:]
    if not files:
        print("暂无系统日志（logs/*/ops.log）。运行任意命令后自动生成。")
        return 0
    if tail is not None:
        files = files[-1:]
    for f in files:
        print(f"=== {f} ===")
        try:
            with open(f, encoding="utf-8") as fh:
                lines = fh.readlines()
            if tail is not None:
                lines = lines[-tail:]
            sys.stdout.writelines(lines)
        except OSError as e:
            print(f"读取失败: {e}", file=sys.stderr)
    return 0


def show_help():
    print(__doc__)
    print("可用命令：")
    width = max(len(k) for k in COMMANDS)
    for name, (_, desc) in COMMANDS.items():
        print(f"  {name:<{width}}  {desc}")
    print("  " + "-" * (width + 2 + len("说明")))
    print(f"  {'logs':<{width}}  查询系统运行日志（logs path|tail N|days N）")
    print(f"  {'list':<{width}}  列出全部命令")
    print(f"  {'test':<{width}}  运行全部单元测试")


def run_tests():
    import glob
    import unittest

    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    test_dir = os.path.join(SRC, "tests")
    sys.path.insert(0, test_dir)
    for path in sorted(glob.glob(os.path.join(test_dir, "test_*.py"))):
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            suite.addTests(loader.loadTestsFromName(name))
        except Exception as exc:  # 收集阶段出错也计入失败
            print(f"加载 {name} 失败: {exc}", file=sys.stderr)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def _run_logged(cmd, args, fn):
    """执行命令并写系统运行日志：开始/结束/耗时/退出码，异常带 traceback。

    目标脚本常直接 sys.exit(code)，这里捕获 SystemExit 取出退出码；其他未捕获异常
    记录 ERROR + traceback 后向上抛（由 logroll 的 excepthook 再兜底落盘）。
    """
    import time
    import logroll

    logroll.ops_start()
    log = logroll.logger("cmd")
    desc = " ".join(str(a) for a in args[:6])
    log.info("BEGIN %s %s", cmd, desc, extra={"file_only": True})
    t0 = time.time()
    rc = 0
    try:
        result = fn()
        if isinstance(result, int):
            rc = result
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException as e:
        log.error("ERROR %s 未捕获异常: %s: %s", cmd, type(e).__name__, e,
                  exc_info=True, extra={"file_only": True})
        logroll.ops_flush()
        raise
    finally:
        log.info("END %s rc=%s elapsed=%.3fs", cmd, rc, time.time() - t0,
                 extra={"file_only": True})
        logroll.ops_flush()
    return rc


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        show_help()
        return 0

    cmd = argv[0]
    if cmd == "list":
        show_help()
        return 0
    if cmd == "logs":
        return show_logs(argv[1:])
    if cmd == "test":
        # 测试自身会拉起各模块，也记一条命令日志，但不展开内部阶段
        return _run_logged("test", argv[1:], lambda: run_tests())
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
    def _exec():
        saved = sys.argv
        try:
            sys.argv = [target] + argv[1:]
            runpy.run_path(target, run_name="__main__")
            return 0
        finally:
            sys.argv = saved

    return _run_logged(cmd, argv[1:], _exec)


if __name__ == "__main__":
    sys.exit(main())
