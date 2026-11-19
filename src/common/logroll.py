"""fate-wheel 系统运行日志（技术侧）：任务耗时、阶段计时、限流/熔断、异常堆栈。

与业务日志隔离：业务结论日志在 data/report/（含 data/report/YYYYMMDD.md、
data/report/{core,eval,signals,calib}/），本模块只写 logs/YYYY-MM-DD/，
两者互不影响，业务日志不参与滚动清理。

设计参考 equipoise/tools/logroll.py，按 fate-wheel 的目录约定调整：
  * ROOT 来自 common/paths.py；
  * root logger 名为 "fw"（equipoise 为 "equipoise"）；
  * 保留天数环境变量 FW_LOG_KEEP_DAYS（默认 90）。

用法：
    import logroll
    log = logroll.logger("snapshot")          # log.info / warning / error(..., exc_info=True)
    with logroll.stage("snapshot", "fetch_quotes"):   # 阶段计时，结束自动记耗时
        ...
    logroll.ops_start()                       # 开启 logs/<日期>/ops.log；所有 logger 同时写文件
                                              # 并挂未捕获异常（主线程+子线程）hook 写 traceback
    logroll.ops_flush()                       # 进程退出前排空队列
    logroll.path("ops.log")                   # 今天的日志文件路径
    logroll.latest("ops.log")                 # 最近一天存在的同名日志
    logroll.find_all("ops.log")               # 所有日期的同名日志（按日期升序）

日志格式：2026-11-19 09:15:28.123 INFO  [MainThread] snapshot - 消息
并只写文件不写控制台：extra={"file_only": True}
"""
import os
import re
import shutil
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from paths import ROOT

SYS_LOGS = Path(ROOT) / "logs"
KEEP_DAYS = int(os.environ.get("FW_LOG_KEEP_DAYS", "90"))
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_lock = threading.Lock()
_cleaned_for = None

_ROOT_LOG = "fw"
_FMT = "%(asctime)s.%(msecs)03d %(levelname)-5s [%(threadName)s] %(mod)s - %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_listener = None


def today_str(t=None):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def cleanup(keep=KEEP_DAYS, today=None):
    """删除 logs/ 下早于「今天往前 keep-1 天」的日期目录，返回被删目录名。只动 YYYY-MM-DD 目录。"""
    today = today or date.today()
    cutoff = (today - timedelta(days=keep - 1)).isoformat()
    removed = []
    if not SYS_LOGS.is_dir():
        return removed
    for d in SYS_LOGS.iterdir():
        if d.is_dir() and _DAY.match(d.name) and d.name < cutoff:
            try:
                shutil.rmtree(d)
                removed.append(d.name)
            except OSError as e:
                print(f"[logroll] 清理 {d} 失败: {e}", file=sys.stderr)
    return removed


def day_dir(t=None):
    global _cleaned_for
    name = today_str(t)
    d = SYS_LOGS / name
    d.mkdir(parents=True, exist_ok=True)
    if t is None and _cleaned_for != name:
        with _lock:
            if _cleaned_for != name:
                _cleaned_for = name
                cleanup()
    return d


def path(name, t=None):
    return day_dir(t) / name


class DailyPath(os.PathLike):
    """按写入时刻解析到当天目录的路径；可直接传给 open()。跨零点自动换到新目录。"""
    def __init__(self, name):
        self.name = name

    def __fspath__(self):
        return str(path(self.name))

    def __str__(self):
        return self.__fspath__()

    def __repr__(self):
        return f"DailyPath({self.name!r})"


def _formatter():
    import logging
    f = logging.Formatter(_FMT, _DATEFMT)
    _fmt = f.format

    def fmt(r):
        if r.levelno == logging.WARNING:
            r.levelname = "WARN"
        r.mod = getattr(r, "mod", None) or r.name.removeprefix(_ROOT_LOG + ".")
        return _fmt(r)

    f.format = fmt
    return f


def logger(name):
    """标准分级日志。INFO 到 stdout，WARN/ERROR 到 stderr；ops_start() 后同时写 ops.log。"""
    import logging
    root = logging.getLogger(_ROOT_LOG)
    if not root.handlers:
        root.setLevel(logging.INFO)
        root.propagate = False
        keep = lambda r: not getattr(r, "file_only", False)
        out = logging.StreamHandler(sys.stdout)
        out.addFilter(lambda r: keep(r) and r.levelno < logging.WARNING)
        err = logging.StreamHandler(sys.stderr)
        err.addFilter(lambda r: keep(r) and r.levelno >= logging.WARNING)
        for h in (out, err):
            h.setFormatter(_formatter())
            root.addHandler(h)
    return logging.getLogger(f"{_ROOT_LOG}.{name}")


class _DailyFileHandler:
    """logging handler：写 logs/<当天>/<name>，跨零点自动换目录。"""
    def __new__(cls, name):
        import logging

        class H(logging.Handler):
            def __init__(self):
                super().__init__()
                self.p, self.cur, self.f = DailyPath(name), None, None

            def emit(self, r):
                try:
                    if str(self.p) != self.cur:
                        if self.f:
                            self.f.close()
                        self.cur = str(self.p)
                        self.f = open(self.cur, "a", encoding="utf-8")
                    self.f.write(self.format(r) + "\n")
                    self.f.flush()
                except Exception:
                    self.handleError(r)

            def close(self):
                if self.f:
                    self.f.close()
                    self.f = None
                super().close()

        return H()


def ops_start(name="ops.log"):
    """开启 logs/<日期>/<name>：之后所有 logger() 的日志同时写文件（队列后台线程，不阻塞主线程）。
    挂主线程与子线程的未捕获异常 hook，以 ERROR + traceback 写入。重复调用无副作用。
    只在命令执行入口（main.py）调用；纯工具/查询脚本只 print。
    """
    global _listener
    if _listener is not None:
        return
    import logging
    import logging.handlers
    import queue
    logger("ops")
    q = queue.Queue()
    fh = _DailyFileHandler(name)
    fh.setFormatter(_formatter())
    listener = logging.handlers.QueueListener(q, fh, respect_handler_level=False)
    root = logging.getLogger(_ROOT_LOG)
    root.addHandler(logging.handlers.QueueHandler(q))
    listener.start()
    _listener = (listener, fh)  # 存两段，flush 只需 stop/start listener

    old = sys.excepthook

    def hook(tp, v, tb):
        logger("uncaught").error("未捕获异常（主线程）: %s: %s", tp.__name__, v,
                                 exc_info=(tp, v, tb), extra={"file_only": True})
        ops_flush()
        old(tp, v, tb)

    sys.excepthook = hook
    old_t = threading.excepthook

    def thook(a):
        logger("uncaught").error(
            "未捕获异常（线程 %s）: %s: %s", a.thread.name if a.thread else "?",
            a.exc_type.__name__, a.exc_value,
            exc_info=(a.exc_type, a.exc_value, a.exc_traceback), extra={"file_only": True})
        ops_flush()
        old_t(a)

    threading.excepthook = thook


def ops_flush():
    """把队列里的日志写完（进程退出前调用）。stop 排空队列后重启以便继续写。"""
    if _listener is not None:
        listener, _ = _listener
        listener.stop()
        listener.start()


class stage:
    """阶段计时上下文：退出时写一条 INFO 'STAGE <scope>.<op> done in X.XXXs'；
    抛异常时写 ERROR 并标注 FAILED + 耗时，再向上抛。

    用法：with logroll.stage("snapshot", "market_status"): ...
    """
    def __init__(self, scope, op, log=None):
        self.scope, self.op = scope, op
        self.log = log or logger(scope)
        self.t0 = None

    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, tp, v, tb):
        elapsed = time.time() - self.t0
        tag = f"STAGE {self.scope}.{self.op}"
        if tp is None:
            self.log.info("%s done in %.3fs", tag, elapsed, extra={"file_only": True})
        else:
            self.log.error("%s FAILED in %.3fs: %s: %s", tag, elapsed, tp.__name__, v,
                           exc_info=(tp, v, tb), extra={"file_only": True})
        return False


def find_all(name):
    if not SYS_LOGS.is_dir():
        return []
    return [d / name for d in sorted(SYS_LOGS.iterdir())
            if d.is_dir() and _DAY.match(d.name) and (d / name).exists()]


def latest(name):
    found = find_all(name)
    return found[-1] if found else SYS_LOGS / today_str() / name
