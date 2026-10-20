# mindmap-engine

默认组合入口，依赖 `mindmap-core`、`mindmap-layouts` 和 `mindmap-formats`，无 Qt 依赖，要求 Python 3.12+。

提供文档模型、四方向布局、11 种分支几何、可回滚事务、差异历史、图片资源管理、
`.mindmap` 与 JSON 读写，以及 Markdown/纯文本大纲交换。

```python
from mindmap_engine import LayoutConfig, MindMapEngine, create_default_layout_registry, save_document

engine = MindMapEngine()
node_id = engine.add_child(engine.root_id, "任务")
engine.update_node(node_id, text="完成任务")
engine.undo()
engine.set_layout(LayoutConfig(direction="down"))
result = create_default_layout_registry().layout(engine.document)
save_document(engine.document, "示例.mindmap")
engine.mark_saved()
```

在工作区运行 `uv build --package mindmap-engine` 可构建独立安装包。
公开接口及关键算法含中文文档字符串。各能力包的说明分别见
[核心](../mindmap-core/README.md)、[布局](../mindmap-layouts/README.md)和[文件格式](../mindmap-formats/README.md)。

所有模型、异常及引擎类型均直接重新导出真实对象。布局通过 `LayoutRegistry` 调度，
`set_layout` 接收完整 `LayoutConfig`；不保留原子模块路径或旧参数形式。
当前文件格式为版本 2，不读取旧版本数据。扩展实现只需依赖核心，不应反向导入本组合包。
组合包重新导出 `BranchStyle`、`PathCommand`、`BranchPath`、`route_branch` 与 `route_branches`，
类型与原包为同一对象。`engine.set_branch_style([node_id], "round", scope="subtree")`
统一提交子树样式，传入 `None` 恢复原有默认曲线。
