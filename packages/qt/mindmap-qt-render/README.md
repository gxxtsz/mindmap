# mindmap-qt-render

思维导图的 Qt 测量、静态绘制与图形导出。本包可以独立构建和安装，不依赖桌面外壳或停靠框架。

要求 Python 3.12+、PySide6 `>=6.9,<7`。宿主先创建 `QApplication`，所有图形接口在 GUI 线程调用。

在仓库根目录执行 `uv build --package mindmap-qt-render` 构建本包；安装 wheel 时由元数据解析依赖。

## 独立导出

```python
from PySide6.QtWidgets import QApplication
from mindmap_core import MindMapDocument
from mindmap_layouts import create_default_layout_registry
from mindmap_qt_render import export_document

app = QApplication([])
document = MindMapDocument.new("独立渲染")
export_document(document, "导图.png", layout_registry=create_default_layout_registry())
```

同一接口支持 `.png`、`.svg`、`.pdf`；`scale` 控制 PNG 分辨率，`expand_all` 控制是否
展开全部节点。输出先在内存完成，再原子替换文件，失败不会截断旧目标。
离屏导出可在创建应用前设置 `QT_QPA_PLATFORM=offscreen`。

`VisualFactory` 维护文字及图片缓存，返回的 `NodeVisual` 只读共享。
`NodeItem` 是静态图元，不持有编辑宿主；交互节点和文本编辑器属于画布包。

本包的运行依赖只有 `mindmap-core` 和 PySide6，不安装内置布局、格式或交互组件。
上述默认树布局示例需另行安装 `mindmap-layouts`；自定义宿主也可只实现核心 `LayoutAlgorithm`
并注册到 `LayoutRegistry` 后传入导出器。独立安装验收使用自定义布局验证三种图形输出。
