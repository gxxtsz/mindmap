# mindmap-qt-workspace

组合画布与业务面板的独立 Qt 停靠工作区。本包可以独立构建和安装，不依赖 `mindmap-qt` 兼容入口、桌面外壳或停靠框架。

要求 Python 3.12+、PySide6 `>=6.9,<7`。宿主先创建 `QApplication`，所有图形接口在 GUI 线程调用。

在仓库根目录执行 `uv build --package mindmap-qt-workspace` 构建本包；安装 wheel 时由元数据解析依赖。

## 组合工作区

```python
from PySide6.QtWidgets import QApplication
from mindmap_qt_workspace import MindMapWorkspace

app = QApplication([])
workspace = MindMapWorkspace()
workspace.resize(1200, 800)
workspace.show()
try:
    app.exec()
finally:
    workspace.dispose()
```

`workspace.canvas` 为完整交互画布，`outline`、`properties`、`settings` 为业务面板。
`save_state()`、`restore_state()` 保持原有版本 1 布局格式，`state_changed` 通知宿主
保存偏好。组件不访问 QSettings，也不替宿主管理导图路径和应用生命周期。

本包面向普通 Qt 宿主。已有桌面停靠和应用外壳的宿主可以分别安装画布与面板包，
按自己的停靠布局装配，不需要安装本包或 `mindmap-qt` 兼容入口。
