"""统一路径与模块搜索路径。

所有子模块原先平铺在项目根目录，靠 __file__ 计算 ROOT 并用裸模块名互相导入。
分层后：
  * ROOT 统一在这里定义，恒为项目根目录（数据/csv/json/logs/cache 的所在处）。
  * setup_sys_path() 把所有代码子目录加入 sys.path，使原有 `import fees`、
    `import signals_engine`、`from datasources import ...` 等裸导入无需改动即可解析。

任何模块只要 `from fw.paths import ROOT`，就会自动触发 setup_sys_path()，
因此即使脱离 main.py 直接运行某个脚本，导入也能正常解析。
"""
import os
import sys

# fw/paths.py 位于 <项目根>/fw/ 下，向上两级即项目根目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FW_DIR = os.path.join(ROOT, "fw")

_CODE_DIRS = [
    ROOT,
    FW_DIR,
    os.path.join(FW_DIR, "core"),
    os.path.join(FW_DIR, "engine"),
    os.path.join(FW_DIR, "data"),      # datasources 包在 fw/data/datasources
    os.path.join(FW_DIR, "agent"),
]


def setup_sys_path():
    """把所有代码目录放到 sys.path 前面，保证裸模块名导入可解析。"""
    for d in _CODE_DIRS:
        if d not in sys.path:
            sys.path.insert(0, d)


setup_sys_path()
