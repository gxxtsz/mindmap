# mindmap-docking

工具窗口可通过 `ToolWindowSpec(..., header_actions=(action, ...))` 在标题栏注册动作，
按顺序展示于框架选项和隐藏按钮之前。带菜单的动作单击即展开，普通动作直接触发，文字和启用状态跟随原动作。
带图标的动作使用28逻辑像素的纯图标按钮和14逻辑像素的图标画布，无图标的动作保留文字；纯图标菜单不额外绘制角落下拉箭头。
业务动作与旁边的选项、隐藏按钮一起显示或隐藏，遵循面板悬停、活动状态及“始终显示标题图标”设置。
默认不添加按钮。动作由业务方持有，须在工具窗口注销前保持有效；框架不接管所有权，也不将动作写入布局快照。
标题按钮随主题刷新，临时展开、停靠和布局恢复时复用同一实例。

思维导图项目内置的 PySide6 停靠与分屏组件，Python 导入名为 `mindmap_docking`。
支持 Python 3.12、3.13，依赖 PySide6 `>=6.12,<6.13`；不依赖桌面外壳、导图画布或导图引擎。

## 安装与构建

本包属于根目录 uv workspace，无需准备其他源码仓库。

```powershell
uv sync --locked --all-packages --group dev
uv build --package mindmap-docking
```

构建后的 wheel 可以独立安装。主题 JSON、SVG 图标及资源摘要随包交付，运行时从本包读取。
编辑器卡片只在外层保留圆角，标签撑满栏高并与正文平直衔接；不绘制标签圆角和整圈描边。
`editor-bg`、`editor-header-bg`、`editor-border` 分别控制正文及当前标签、标签栏、分隔线；
当前标签底部使用2逻辑像素的 `control-brand-border` 蓝线，文件图标为14逻辑像素，标题使用常规字重。
`editor_header_height`／`compact_editor_header_height` 控制标签行高度，默认32／28逻辑像素。
业务文档控件应自行铺满正文；需要跟随外壳底色时由宿主显式传递，框架不改写业务数据。

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
- `set_tools_suspended(ids, suspended)` 根据业务上下文临时暂停或恢复工具，暂停时隐藏面板和入口并禁用快捷键，保留原有布局偏好；暂停状态不写入快照，布局恢复不会解除暂停。`is_tool_available(id)` 查询综合可用性。
- `DocumentSpec` 注册文档，支持取消关闭、修改标记、主窗口内分屏及标签迁移；`dispose_on_close=False` 默认关闭后缓存重用，设为 `True` 则关闭后移除注册并延迟销毁控件。
- `active_document_id`、`document_activated` 跟踪活动文档；`activate_document(id)` 只切换原标签和焦点，不移动分屏位置，侧栏焦点不覆盖活动文档。
- `close_documents(ids)` 先检查全部目标，全部通过后才移除页面；`on_closed` 可停止业务计时器和订阅。
- `host.editor.set_empty_widget_factory(factory)` 给每个空分组创建独立页面，空白页不占用标签、文档标识或快照。
- `DockPosition`、`DockTarget`、`ViewMode` 描述停靠位置、移动目标与显示模式；宿主仅允许 `main`，模式仅保留固定停靠、取消固定停靠和主窗口内临时展开。
- `set_document_actions(document_id, actions)` 在当前文档标签栏右侧复用已有 `QAction`；空序列清除入口。
- `capture_layout()`、`restore_layout(snapshot)` 操作版本 1 的布局快照，恢复仅接受字典，业务文档内容不进入布局。
- 构造参数 `preserve_open_documents=False` 默认维持按快照恢复行为；设为 `True` 则过滤未知文档标识，保留全部当前打开文档和活动文档，缺失于快照的文档按原编辑区顺序追加。恢复前先校验，不修改调用方快照。
- `set_default_layout()` 记录宿主初始排布，`restore_default_layout()` 或 `Shift+F12` 恢复该模板；未建立模板时不改变状态。
- 会话自动保存和恢复保留当前排布；命名布局的保存、列举、重命名、删除及弹窗保存接口已移除。
- `Theme`、`ThemeTokens`、`IconRegistry` 提供明暗主题、局部覆盖与图标注入。

文档动作继续由业务持有，必须在清除入口或注销文档前保持有效。按钮自动跟随动作的启用状态、提示和图标，
切换标签、分屏、迁移和重开时自动同步，不复制动作或另外注册快捷键。
参数、异常、所有权和生命周期说明见[公共模型](src/mindmap_docking/models.py)、
[管理器](src/mindmap_docking/manager.py)、[扩展协议](src/mindmap_docking/storage.py)。

默认用户配置位于 Qt 用户配置位置下的 `mindmap/<application_id>/`，详见[桌面框架说明](../../docs/桌面框架.md)。
资源与 MIT 许可见[资源与许可](../../docs/资源与许可.md)和 [LICENSE](LICENSE)。

## 单主窗口与历史会话

辅助窗口及浮动／独立面板能力已删除，不再提供 `create_frame`、`detach_token`、`close_frame`、
`frame_factory`、`place_floating`、`visible_geometry`，也不再提供 `ViewMode.FLOAT`、`ViewMode.WINDOW`。
面板最大化、全部隐藏／恢复、工作区导航接口及对应动作一并删除；单面板显示、隐藏和基本停靠继续支持。
`focus_editor`、`focus_workspace`、`focus_last_tool`、`hide_active`、`hide_restore_all`、`toggle_maximize` 不再公开。
隐藏活动面板时内部交还文档焦点，不注册 Escape、Shift+Escape、F12、F6 或面板最大化快捷键；
`Ctrl+Shift+F12` 的全部隐藏／恢复操作已删除，`Shift+F12` 恢复默认布局继续保留。

历史快照仍按版本 1 读取，旧 `float`／`window` 只在迁移层识别并转换为固定停靠。
辅助宿主标签按保存顺序追加到主窗口当前组并去重，主分屏和当前标签保持不变；
旧文件没有当前组信息时优先使用活动工具标签所属组，否则使用第一个标签组。
浮动面板优先返回最后停靠位置；与主窗口可见面板冲突时优先寻找同边空位，再使用其他空位；六区全满时追加为编辑器标签，保留打开状态。
未注册工具也迁移到 `main`，保留隐藏状态，稍后注册不会恢复旧窗口模式。
正常保存仅写主宿主，不再保存面板浮窗几何，不额外修改或删除用户配置文件。
