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

## 公开图标接口

`toolbar_icon(name, foreground, disabled, accent) -> QIcon` 按名称及三种颜色生成、缓存图标。
颜色分别用于普通、禁用和选中状态；缓存包含多档设备像素比。未知名称抛出 ValueError。
调用前须有 QApplication，并在 GUI 线程使用；返回图标供动作或按钮展示，不持有引擎或文档。
