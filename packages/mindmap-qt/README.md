# mindmap-qt

基于 PySide6 的原生思维导图组件，依赖 `mindmap-engine`，不依赖 Harbor。
独立包要求 Python 3.12+、PySide6 `>=6.9,<7`；当前工作区与工作台共同使用 PySide6 6.12.0 验收。

```python
from PySide6.QtWidgets import QApplication
from mindmap_engine import MindMapEngine
from mindmap_qt import MindMapWidget

app = QApplication([])
widget = MindMapWidget(MindMapEngine())
widget.resize(1100, 750)
widget.show()
try:
    app.exec()
finally:
    # 宿主退出时主动释放引擎订阅及未完成下载。
    widget.dispose()
```

组件提供节点、图片和 11 种父子分支的绘制及 PNG/SVG/PDF 导出。
专用剪贴板使用当前版本 JSON，与文件打开共用格式校验，不读取旧版本数据。

构造函数接受 `layout_registry` 和 `format_registry`，省略时分别创建独立默认实例。
画布与图形导出使用同一个布局注册表，剪贴板使用注入的 JSON、Markdown 和纯文本实现。
自定义格式注册表需保留这三个标识才能使用完整剪贴板能力。
通过 `widget.set_layout(config)` 切换布局，会在提交历史前校验并试算；加载新文档前可调用
`widget.validate_document(document)`，确认算法、图片和几何结果有效后再替换引擎文档。

宿主负责 QApplication 生命周期。组件支持多实例、只读模式、事件信号和菜单扩展。
内置工具栏使用 SVG 图标，悬停显示中文功能名称和当前快捷键，支持明暗主题及高 DPI 显示。
图标按钮复用原有动作，菜单名称、快捷键和只读限制保持一致。
默认工具栏提供撤销、重做、适应窗口和回到中心；“添加子节点”可通过右键菜单或 Tab 快捷键调用，
宿主仍可复用 `widget.action("add_child")` 或调用 `widget.add_child()`。
节点属性可从右键菜单或 Alt+Enter 打开；宿主也可接管该入口并显示属性侧栏。
分支连接线选择位于节点属性的“分支”分组。
按住右键拖动可平移画布，右键单击保留菜单，中键或 Alt+左键也可平移。
画布始终隐藏横向与纵向滚动条，内容增加时自动扩展，删除、折叠或聚焦后按当前
可见内容重新计算并收缩。四周各保留一屏操作余量，随窗口大小和缩放比例换算；
拖动或滚轮到达边缘后停止移动，不会继续扩展空白区域。自动调整保持缩放比例和
有效范围内的视口中心，中心越界时移到最近边界。
“适应窗口”按内容范围计算，“回到中心”定位根节点或当前聚焦节点；缩放动作围绕
视口中心进行，滚轮缩放在有效范围内保持鼠标锚点。导航余量不计入图片或文档导出。
全局设置侧栏切换滚轮上下滚动或缩放，默认上下滚动；Ctrl+滚轮始终围绕鼠标缩放。
独立画布保留 `widget.action("wheel_mode")` 及其图标菜单，宿主可加入自己的菜单或工具栏。
用 `wheel_mode` 查询模式、`set_wheel_mode("scroll" / "zoom")` 设置模式，并通过
`wheel_mode_changed(str)` 监听实际变化。模式独立于文档和撤销历史，只读时也可切换；
持久化由宿主管理，组件自身不读写本机偏好。

节点属性单页中的“分支”分组通过图标下拉入口打开四列十一项圆角网格，提供中文提示、当前项高亮及恢复默认曲线。
全局设置、节点属性及属性弹窗共用约 268×230 逻辑像素的紧凑弹层，保留右侧对齐、阴影和屏幕边缘避让。
支持整个子树或仅本级，悬停显示提示，关闭菜单不修改样式。
侧栏点击后自动提交；弹窗按“确定”与外观一起提交一次历史，取消不修改文档；未改样式不覆盖后代。
`widget.set_branch_style("round", scope="subtree")` 使用当前选择，无选择时使用聚焦主题或根主题；
`scope="level"` 仅改本级，`None` 恢复默认。只读、文字编辑和不支持此能力的布局禁用入口。
画布、选择器和导出复用正式路由与描画，旧 `Curve` 布局扩展保持兼容。
`widget.action("properties")` 和 `widget.action("branch_style")` 继续可供宿主复用；后者保留四列菜单和临时预览能力。
在工作区运行 `uv build --package mindmap-qt` 构建独立安装包。

## 独立 Qt 停靠工作区

使用 `from mindmap_qt import MindMapWorkspace` 创建包含原画布、完整大纲、节点属性及全局设置的工作区，
其构造参数与画布一致，原画布可以通过 `workspace.canvas` 访问。
工作区可嵌入普通 Qt 布局，也可作为 `QMainWindow` 的中央控件；无需新增界面依赖。
Harbor 工作台通过下文的 `MindMapPanels` 接入框架，由 Harbor 管理外壳与停靠布局。

工作区采用卡片分区：浅色外围为 `#E9EAEE`、三个侧栏为 `#F5F6F8`，深色外围为 `#1E1F22`、侧栏为 `#2B2D30`。
大纲、节点属性和全局设置的标题、内容、滚动区空白及底部圆角留白使用同一底色，左侧、右侧、底部停靠和浮动状态保持一致。
输入框保留浅色白底及深色 `#303237`；独立属性弹窗与菜单沿用原有底色。样式原地刷新，保留焦点、光标、滚动和未提交输入。
卡片圆角 8、间隙 6、按钮栏 40、标题栏 36，均为逻辑像素；界面沿用当前文档的文字色和强调色。
内部 `ui_style` 统一配色、控件及菜单样式，
只作用于组件自身，不修改 QApplication 或宿主的全局样式。文件和颜色选择对话框仍由宿主与 Qt 管理。
中央画布卡片仍使用 `Theme.background`，节点和导出继续读取原文档主题；卡片边角、外围及间隙不参与导出。
独立 `MindMapWidget` 同样使用圆角画布承载层，原 `view`、`scene` 和动作接口不变。

侧边按钮支持左、右、底部停靠、同侧排序及拖出浮动；关闭浮动窗口只隐藏面板。
默认右侧展开节点属性，大纲和全局设置隐藏。每个区域同时展示一个面板，不同区域可以同时展开。
大纲树使用 16 逻辑像素缩进、28 行高和 12 像素正文，使用实心三角、灰色叶节点圆点及连续的祖先竖线。
大纲树区沿用所有侧栏共用的底色，保持连续的卡片表面。
选中态沿用主题的柔和强调色，内置浅色和深色分别为 `#DFE9F8`、`#374251`；文字、三角和叶节点随工作区配色。
大纲支持像素滚动、原生键盘展开及多选定位，树自身的展开与收起不改写画布折叠状态。
定位被折叠隐藏的主题时，会在一次事务中展开所需祖先；只读模式拒绝需要展开文档的定位。
节点属性共享独立画布的单页表单，按字体、主题、分支分组，支持字体搜索、字号增减、色块选择和自定义最大文本宽度。
没有 Tab 或内容编辑字段，已有正文、备注、标签等数据完整保留；正文继续在画布中原地编辑。
节点属性侧栏自动提交：离散选项立即提交，字体名称和数值等待 400 毫秒或失焦后提交，
多选只更新实际修改的样式及分支字段。

`show_tool_window("outline" / "properties" / "settings", focus=True)` 展开面板，`hide_tool_window(...)` 隐藏，
`restore_state(None)` 恢复默认布局。`save_state()` 返回普通字典，宿主监听 `state_changed`
并自行保存本机偏好；组件不访问 QSettings。保存或导出文档前调用 `flush_pending_edits()`。
直接调用 `workspace.canvas.export(...)` 也会先通知侧栏提交有效输入。
该画布接口导出正式文档，不提交原地文字草稿；需要包含草稿时，宿主应先调用
`workspace.canvas.finish_edit(commit=True)`。文件控制器会统一完成这一步。
宿主永久移除工作区时调用 `dispose()` 取消未完成输入、拖动和网络请求；临时隐藏面板时保留实例。

`MindMapWidget.set_properties_handler(handler)` 可由宿主接管属性入口，传入 `None` 恢复弹窗。
独立画布仍可使用属性弹窗，应用菜单与文件操作由宿主提供。

## 全局设置

在独立 Qt 工作区中通过右侧齿轮按钮、Alt+3 或 `workspace.show_tool_window("settings")` 打开，
面板可通过 `workspace.settings` 访问；组件不自带应用菜单。
Harbor 工作台将同一面板注册为“导图设置”，菜单由 Harbor 外壳提供。
默认右侧收起，宽度 360 逻辑像素，分为滚轮、布局、分支和外观四组，共用一个滚动区。
布局方向、紧凑布局、分支和主题从当前导图读取，修改后支持撤销、重做及保存重开。
主题支持浅色和深色；已有自定义主题显示“自定义”，打开或刷新面板不会替换它。

全局分支选择复用四列十一项网格，始终覆盖整张导图，包括折叠、聚焦范围外及叶节点，
不受当前选择和局部作用范围影响；恢复默认曲线也作用于整图。新增子节点、同级节点及父节点沿用现有继承规则。
局部分支设置继续保留，出现差异时全局入口显示“多种样式”，再次选择全局样式可统一。
移动和粘贴保留节点原有配置，不进行隐式覆盖。一次全局修改只产生一条撤销记录，其他节点属性保持原值。

提交文档设置前先保存节点属性中的有效待提交输入，并验证完整候选文档；失败后恢复控件显示并通过错误信号报告。
只读或文字编辑期间禁用文档设置，滚轮仍可调整；不支持的布局能力单独禁用并提供中文提示。
`MindMapWorkspace.save_state()` 返回版本 1 的布局字典。
该版本只描述独立 Qt 工作区布局，与导图文件版本 2、Harbor 会话布局分别管理。


## 独立宿主集成

`MindMapPanels(canvas, parent=None)` 为一个画布提供 `outline`、`properties`、`settings`
三个业务面板，宿主将它们放入自己的布局或停靠容器。
组合负责大纲定位、选择同步和属性提交，使用 `flush_pending_edits()` 提交有效属性输入。
退出宿主时先调用 `panels.dispose()`，再调用 `canvas.dispose()`；
二者均可重复调用。面板组合的清理不销毁 QWidget，控件归属和最终销毁由宿主负责。

`MindMapFileController(canvas, panels, dialog_parent=None, *, writer=...)`
提供宿主无关的文件生命周期，不读写本机偏好，也不拥有画布。
`path` 表示当前保存位置，`is_dirty` 同时包含引擎修改状态与有损导入待保存状态。
`new_document()`、`open_path()`、`save()` 返回是否完成操作，
`confirm_replace()` 统一处理保存确认；主文档只使用可完整往返的格式。
保存、导出和确认替换前，控制器会提交有效属性输入与原地文字。
文件解析和画布验证成功后才询问是否替换；写入成功后才更新路径与保存标记。
`export_path()` 导出文档快照，不更新当前保存位置，也不清除未保存标记。
`open_path()`、`export_path()` 的异常交给宿主处理；
`save()` 写入失败返回 False，并通过 `error_occurred(str)` 报告。
`state_changed` 通知文件状态变化，`message(str)` 提供导出结果提示。

`MindMapWidget.set_ui_colors(colors)` 与
`MindMapPanels.set_ui_colors(colors)` 接受 `UiColors` 或 None，可覆盖界面控件和菜单色板，
不改变导图主题、撤销历史或图形导出；传入 None 恢复默认的文档主题派生配色。
独立 Qt 工作区上文列出的色值属于默认配色，Harbor 工作台会按外壳主题映射界面色板。
公共接口的完整签名及生命周期说明见[面板组合](src/mindmap_qt/panels.py)、
[文件控制器](src/mindmap_qt/files.py)；框架接入示例见
[Harbor 适配层](../../apps/mindmap-workbench/src/mindmap_workbench/harbor_adapter.py)。
