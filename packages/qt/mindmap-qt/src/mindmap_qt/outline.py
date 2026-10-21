"""兼容原 mindmap_qt.outline 路径；真实实现由独立功能包提供。

直接复用真实模块对象，保证原导入的类型、模块变量及宿主的运行时替换仍然一致。
新代码可以直接导入 mindmap_qt_panels.outline，按需安装对应功能包。
"""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("mindmap_qt_panels.outline")
