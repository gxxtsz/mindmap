# mindmap-qt-panels

通过公开宿主协议连接的大纲、属性与设置面板。本包可以独立构建和安装，不依赖 `mindmap-qt` 兼容入口、桌面外壳或停靠框架。

要求 Python 3.12+、PySide6 `>=6.9,<7`。宿主先创建 `QApplication`，所有图形接口在 GUI 线程调用。

在仓库根目录执行 `uv build --package mindmap-qt-panels` 构建本包；安装 wheel 时由元数据解析依赖。

## 宿主协议

公开入口包括 `MindMapPanels`、`OutlinePanel`、`NodePropertiesPanel`、
`GlobalSettingsPanel` 和 `PanelHost`。宿主提供协议中的文档、选择、布局校验和信号，
不需要继承或安装 `MindMapWidget`；普通 QWidget 宿主也可以连接这些面板。

`MindMapPanels(host, parent=None)` 生成 `outline`、`properties`、`settings` 三个业务对象。
大纲和属性由宿主放入布局，设置通过 `settings.create_menu(parent)` 加入宿主菜单。
保存或关闭前调用 `flush_pending_edits()`；退出前调用 `dispose()`，再释放宿主。

`PanelHost` 源码详细说明参数、返回值、异常、借用数据和信号顺序。面板只读使用
`document_view`、`parent_index`，不会访问宿主私有字段、场景或图元。候选验证与
正式事务分开，外部文档变化会取消过期输入，输入法候选保持原有提交语义。

仓库中的 `tests/test_host_protocol.py` 提供不使用画布的完整宿主实现与回归示例。
