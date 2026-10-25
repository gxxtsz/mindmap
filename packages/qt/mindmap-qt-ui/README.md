# mindmap-qt-ui

思维导图的共享 Qt 控件、属性表单与弹窗。本包可以独立构建和安装，不依赖桌面外壳或停靠框架。

要求 Python 3.12+、PySide6 `>=6.9,<7`。宿主先创建 `QApplication`，所有图形接口在 GUI 线程调用。

在仓库根目录执行 `uv build --package mindmap-qt-ui` 构建本包；安装 wheel 时由元数据解析依赖。

## 共享表单

```python
from PySide6.QtWidgets import QApplication
from mindmap_core import MindMapDocument
from mindmap_qt_ui import NodePropertiesForm

app = QApplication([])
document = MindMapDocument.new("表单预览")
form = NodePropertiesForm()
form.load_nodes([document.root], root_id=document.root_id, parents={})
form.show()
app.exec()
```

表单只显示数据并通过 `edited`、`flush_requested` 等信号通知宿主，不自行建立引擎
事务或写入文档。侧栏防抖提交属于面板包，画布属性弹窗由画布控制器确认后提交。
`UiColors` 只控制组件界面，文档主题及图形导出不随宿主配色被改写。

共享表单提供 `set_ui_colors(UiColors | None)` 设置独立色板，传入 `None` 恢复最近一次
`set_theme()` 的主题；`set_branch_scope("subtree" | "level")` 只同步范围展示，不发出
编辑信号。`close_popups()` 关闭表单拥有的分支、字体和补全弹层，`composing` 查询
是否仍有输入法候选。这些操作保留草稿、字段及文档，宿主不需要访问加载标记、
色板私有字段或具体补全器。

未选中节点时，表单在内容区居中偏上的位置展示节点连接图标、“选择一个节点”及
“点击画布中的节点，查看和编辑属性”说明。提示随面板宽度换行、随宿主主题配色；
选中节点后切换为左对齐的节点名称和属性表单。

其他共享组件包括分支选择器、字体选择器、颜色按钮、图标及粘贴选项弹窗。
本包使用渲染包生成正式分支预览，不导入交互画布或业务面板。

外部大纲共用弹窗 `edraw_paste_options()` 返回 `EdrawPasteOptions(mode, keep_children, replace_root=True)`，
取消返回 `None`；沿用亿图接口及控件标识以保持兼容，只收集选择，不修改文档或本机配置。
可选参数 `title` 默认“粘贴亿图脑图”；画布识别文本后分别传入“粘贴缩进大纲”或“粘贴 Markdown 大纲”，
三种来源复用相同的卡片、按钮、结果说明和明暗主题样式。替换模式可分别选择是否替换当前选中主题的文字、
是否保留其原有后代；保留根节点时仅导入复制内容的子树，并保留当前主题文字和格式。
追加模式隐藏并禁用这两组选择，切回替换后恢复原选择；多个并列来源主题只能追加。

`LayoutPicker` 使用120×34逻辑像素的“小图标＋当前方向文字＋箭头”入口，布局菜单宽144逻辑像素、每行高28，
菜单右边缘对齐入口，并关闭系统投影；四方向选项保留图标、中文名称及右侧勾选。紧凑样式仅用于布局选择器。
控件保留组合框值模型和 `activated` 信号；
`set_value()` 同步正式值，取消弹层或更新主题不会发送用户选择。`SpacingSpinBox` 提供一位小数输入和
`composing`、`composition_finished` 输入法状态，提交与防抖由业务面板管理；
目标文档失效时调用 `discard_composition()` 丢弃本字段候选，不提交或影响其他输入控件。

## 公开图标接口

`toolbar_icon(name, foreground, disabled, accent) -> QIcon` 按名称及三种颜色生成、缓存图标。
颜色分别用于普通、禁用和选中状态；缓存包含多档设备像素比。未知名称抛出 ValueError。
调用前须有 QApplication，并在 GUI 线程使用；返回图标供动作或按钮展示，不持有引擎或文档。
