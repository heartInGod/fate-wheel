"""统一路径与模块搜索路径。

所有代码模块位于项目根的 src/ 下（src/common/ / src/core/ / src/engine/ / src/agent/ /
src/datasources/），靠 __file__ 计算 ROOT 并用裸模块名互相导入：
  * ROOT 统一在这里定义，恒为项目根目录（data/、logs/、cache/ 的所在处；
    业务日志在 data/report/，技术运行日志在 logs/）。
    paths.py 位于 <ROOT>/src/common/ 下，因此 ROOT 需要上溯三级。
  * setup_sys_path() 把所有代码子目录加入 sys.path，使原有 `import fees`、
    `import signals_engine`、`from datasources import ...` 等裸导入无需改动即可解析。

任何模块只要 `from paths import ROOT`，就会自动触发 setup_sys_path()，
因此即使脱离 main.py 直接运行某个脚本，导入也能正常解析。
"""
import os
import sys

# paths.py 位于 <ROOT>/src/common/ 下，上溯三级即项目根目录
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src")

_CODE_DIRS = [
    ROOT,                              # data/、logs/、cache/ 及运行时产物
    SRC,                               # src/datasources/ 等包
    os.path.join(SRC, "common"),       # paths.py、datamul.py 等基础设施
    os.path.join(SRC, "core"),
    os.path.join(SRC, "engine"),
    os.path.join(SRC, "agent"),
    os.path.join(SRC, "research"),
]


def setup_sys_path():
    """把所有代码目录放到 sys.path 前面，保证裸模块名导入可解析。"""
    for d in _CODE_DIRS:
        if d not in sys.path:
            sys.path.insert(0, d)


setup_sys_path()
