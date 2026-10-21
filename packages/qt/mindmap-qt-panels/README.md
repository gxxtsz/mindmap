# mindmap-qt-panels

通过公开宿主协议连接的大纲、属性与设置面板。本包可以独立构建和安装，不依赖桌面外壳或停靠框架。

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

## 面板组合与生命周期

```python
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_panels import MindMapPanels

# 宿主应先创建 QApplication；此示例额外使用独立安装的画布包。
canvas = MindMapWidget()
panels = MindMapPanels(canvas)
# 将 panels.outline、panels.properties 放入宿主布局，设置菜单由宿主承载。
settings_menu = panels.settings.create_menu(canvas)
```

`outline` 提供 `display_depth`、`set_display_depth()`、`display_depth_changed` 和 `depth_action`；
None 表示所有层，1～9 表示指定层数，根主题计第1层。展开状态不修改导图折叠和历史。
`properties` 按字体、主题、分支分组，离散项立即提交，文本与数值等待400毫秒或失焦；
只提交实际修改的字段，输入法候选不被保存、导出或外部刷新强制确认。

`settings.create_menu(parent)` 提供滚轮、布局方向、紧凑布局、整图连接线及导图主题。
设置提交前请求有效属性输入，通过候选校验后写入一次事务；只读或文字编辑期间只允许调整滚轮。
菜单只展示当前值，不自动更改自定义主题。设置对象不注册独立侧栏，菜单显示位置由宿主决定。

保存前调用 `flush_pending_edits()`；永久退出时先 `panels.dispose()`，再释放宿主或画布。
面板协调器不销毁 QWidget，最终控件所有权由宿主布局管理。
`set_ui_colors(UiColors | None)` 只刷新展示，不提交草稿、不改写导图主题或历史。
