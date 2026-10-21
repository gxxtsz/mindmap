# mindmap-qt-canvas

可独立嵌入的 Qt 思维导图交互画布与文件控制器。本包可以独立构建和安装，不依赖 `mindmap-qt` 兼容入口、桌面外壳或停靠框架。

要求 Python 3.12+、PySide6 `>=6.9,<7`。宿主先创建 `QApplication`，所有图形接口在 GUI 线程调用。

在仓库根目录执行 `uv build --package mindmap-qt-canvas` 构建本包；安装 wheel 时由元数据解析依赖。

## 嵌入画布

```python
from PySide6.QtWidgets import QApplication
from mindmap_qt_canvas import MindMapWidget

app = QApplication([])
canvas = MindMapWidget()
canvas.resize(1000, 700)
canvas.show()
try:
    app.exec()
finally:
    canvas.dispose()
```

`MindMapWidget` 保留原构造参数、动作、信号与公开方法。支持独立引擎、多实例、
只读、增量布局、视口虚拟化、原地编辑、拖动、剪贴板、图片导入及图形导出。
宿主负责应用事件循环和清理，组件不保存本机偏好。

面板可以通过 `document_view`、`parent_index`、`is_editing`、`branch_scope`、
`branch_kinds()`、`reveal_nodes()` 等公开接口集成。借用文档禁止修改，查询不会
复制整图；候选编辑应另取引擎快照并调用 `validate_document()`。

`MindMapFileController(canvas, panels, dialog_parent=None, *, writer=...)` 保持原签名。
第二参数仅要求提供 `flush_pending_edits()`，无侧栏时可以为空实现，不要求安装面板包。
退出宿主前调用 `dispose()` 解除引擎订阅并取消未完成下载；该方法可重复调用。
