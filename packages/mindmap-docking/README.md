# mindmap-docking

工具窗口可通过 `ToolWindowSpec(..., header_actions=(action, ...))` 在标题栏注册动作，
按顺序展示于框架选项和隐藏按钮之前。带菜单的动作单击即展开，普通动作直接触发，文字和启用状态跟随原动作。
带图标的动作使用28逻辑像素的纯图标按钮和14逻辑像素的图标画布，无图标的动作保留文字；纯图标菜单不额外绘制角落下拉箭头。
业务动作与旁边的选项、隐藏按钮一起显示或隐藏，遵循面板悬停、活动状态及“始终显示标题图标”设置。
默认不添加按钮。动作由业务方持有，须在工具窗口注销前保持有效；框架不接管所有权，也不将动作写入布局快照。
标题按钮随主题刷新，浮动、停靠和布局恢复时复用同一实例。

思维导图项目内置的 PySide6 停靠与分屏组件，Python 导入名为 `mindmap_docking`。
支持 Python 3.12、3.13，依赖 PySide6 `>=6.12,<6.13`；不依赖桌面外壳、导图画布或导图引擎。

## 安装与构建

本包属于根目录 uv workspace，无需准备其他源码仓库。

```powershell
uv sync --locked --all-packages --group dev
uv build --package mindmap-docking
```

构建后的 wheel 可以独立安装。主题 JSON、SVG 图标及资源摘要随包交付，运行时从本包读取。

## 接入现有 Qt 窗口

```python
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow
from mindmap_docking import DockHost, DockManager, DocumentSpec

app = QApplication([])
window = QMainWindow()
manager = DockManager(application_id="example-desktop")
host = DockHost(manager, "main", window)
window.setCentralWidget(host)
manager.register_document(DocumentSpec("document", "示例文档", lambda: QLabel("业务内容")))
window.show()
try:
    app.exec()
finally:
    # 先解除全局事件监听和延迟保存，再由 Qt 父子关系销毁控件。
    manager.shutdown()
```

调用方拥有 `QApplication`、主窗口及业务数据；管理器协调控件的归属和迁移。
所有界面操作在 Qt GUI 线程调用，框架默认只修改自己的局部主题。

## 公共能力

- `ToolWindowSpec` 注册工具面板，内容工厂延迟创建且仅创建一次，关闭或迁移保留实例。
- `DocumentSpec` 注册文档，支持取消关闭、修改标记、关闭后重开、分屏及跨窗口迁移。
- `DockPosition`、`DockTarget`、`ViewMode` 描述停靠位置、移动目标与显示模式。
- `set_document_actions(document_id, actions)` 在当前文档标签栏右侧复用已有 `QAction`；空序列清除入口。
- `capture_layout()`、`restore_layout(snapshot)` 操作版本 1 的布局快照，恢复仅接受字典，业务文档内容不进入布局。
- `set_default_layout()` 记录宿主初始排布，`restore_default_layout()` 或 `Shift+F12` 恢复该模板；未建立模板时不改变状态。
- 会话自动保存和恢复保留当前排布；命名布局的保存、列举、重命名、删除及弹窗保存接口已移除。
- `Theme`、`ThemeTokens`、`IconRegistry` 提供明暗主题、局部覆盖与图标注入。

文档动作继续由业务持有，必须在清除入口或注销文档前保持有效。按钮自动跟随动作的启用状态、提示和图标，
切换标签、分屏、迁移和重开时自动同步，不复制动作或另外注册快捷键。
参数、异常、所有权和生命周期说明见[公共模型](src/mindmap_docking/models.py)、
[管理器](src/mindmap_docking/manager.py)、[扩展协议](src/mindmap_docking/storage.py)。

默认用户配置位于 Qt 用户配置位置下的 `mindmap/<application_id>/`，详见[桌面框架说明](../../docs/桌面框架.md)。
资源与 MIT 许可见[资源与许可](../../docs/资源与许可.md)和 [LICENSE](LICENSE)。
