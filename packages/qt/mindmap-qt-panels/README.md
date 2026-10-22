# mindmap-qt-panels

通过公开宿主协议连接的大纲、属性与设置面板。本包可以独立构建和安装，不依赖桌面外壳或停靠框架。

要求 Python 3.12+、PySide6 `>=6.9,<7`。宿主先创建 `QApplication`，所有图形接口在 GUI 线程调用。

在仓库根目录执行 `uv build --package mindmap-qt-panels` 构建本包；安装 wheel 时由元数据解析依赖。

## 宿主协议

公开入口包括 `MindMapPanels`、`OutlinePanel`、`NodePropertiesPanel`、
`GlobalSettingsPanel` 和 `PanelHost`。宿主提供协议中的文档、选择、布局校验和信号，
不需要继承或安装 `MindMapWidget`；普通 QWidget 宿主也可以连接这些面板。

`MindMapPanels(host, parent=None)` 生成 `outline`、`properties`、`settings` 三个业务对象。
三个面板均可由宿主放入布局，设置还通过 `settings.create_menu(parent)` 加入宿主菜单。
保存或关闭前调用 `flush_pending_edits()`；退出前调用 `dispose()`，再释放宿主。

`is_composing` 只读汇总面板的输入法候选状态，保存或关闭前须等待候选完成；
`close_popups()` 统一关闭大纲、属性、设置的菜单和补全弹层，不强制提交候选。
宿主通过这两个接口管理会话切换，无需依赖字段键名、具体控件或菜单对象。
`set_ui_colors(UiColors | None)` 使用各面板的公开色板接口，传入 `None` 恢复文档主题。
释放后的 `is_composing` 返回 `False`，重复关闭弹层不再访问控件。

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
# 将 panels.outline、panels.properties、panels.settings 放入宿主布局。
# 将此菜单直接加入宿主“设置”，滚轮标题会显示当前值。
wheel_menu = panels.settings.create_menu(canvas)
# 导图设置面板的显示由宿主侧边按钮或快捷键安排。
```

`outline` 提供 `display_depth`、`set_display_depth()`、`display_depth_changed` 和 `depth_action`；
None 表示所有层，1～9 表示指定层数，根主题计第1层。展开状态不修改导图折叠和历史。
`properties` 按字体、主题、分支分组，离散项立即提交，文本与数值等待400毫秒或失焦；
只提交实际修改的字段，输入法候选不被保存、导出或外部刷新强制确认。

`settings` 侧栏分为“导图布局”和“导图外观”：前者提供布局结构与主题间距，后者提供整图连接线样式与导图主题。
布局入口以小图标和当前方向文字紧凑显示；连接线复用四列图形选择器，主题提供浅色、深色及只读的自定义状态。
外观字段标签在左、选择器靠右，窄栏自动换行并共用纵向滚动区；面板底部不显示说明标签，不支持的设置通过禁用状态和工具提示说明原因。
`settings.create_menu(parent)` 保留调用签名，仅返回“滚轮行为：当前值”菜单，直接包含上下滚动和缩放两个互斥选项；菜单不再提供布局或外观入口。
面板显示由宿主的按钮或快捷键安排，不再提供 `layout_panel_requested` 信号；独立宿主无需安装桌面外壳。
主题间距预设为父子／同级的32／10、64／22、96／36，水平与竖直字段随向下布局转置显示。
数值输入等待400毫秒或回车、失焦后提交，宿主可调用 `settings.flush()` 提交，`discard_pending()` 取消，`composing` 查询候选状态。
组合的 `flush_pending_edits()`、`is_composing`、`dispose()` 已统一处理属性和布局输入，切换导图前先提交有效输入，再关闭弹层。
设置提交前请求有效属性输入，通过候选校验后写入一次事务；只读或文字编辑期间只允许调整滚轮。
查看面板或菜单只展示当前值，不自动更改自定义主题。全局连接线选择会覆盖整张导图的局部样式；混合状态显示“多种样式”，重新选择同一种样式仍可统一全图。
设置对象不自行注册停靠窗口，面板及菜单位置均由宿主决定。

保存前调用 `flush_pending_edits()`；永久退出时先 `panels.dispose()`，再释放宿主或画布。
面板协调器不销毁 QWidget，最终控件所有权由宿主布局管理。
`set_ui_colors(UiColors | None)` 只刷新展示，不提交草稿、不改写导图主题或历史。
