# mindmap-core

思维导图核心，包含模型、校验、编辑操作、事务、差异历史、事件、共享几何类型与扩展协议。
仅使用 Python 3.12+ 标准库，不导入布局实现、文档格式、默认组合包或 Qt。

```python
from mindmap_core import LayoutConfig, MindMapEngine

engine = MindMapEngine()
engine.add_child(engine.root_id, "任务")
engine.set_layout(LayoutConfig(algorithm_id="自定义算法", options={"间距": 24}))
engine.undo()
```

文档可以保存尚未安装的算法配置。核心只验证配置结构，算法的选项语义由实现校验。
需要撤销与事件通知的编辑应通过引擎提交，`engine.document` 和 `engine.get_node(node_id)` 返回快照；
直接修改这些快照不会写回引擎。嵌套操作失败会导致整组事务回滚。
第三方扩展实现 `LayoutAlgorithm`、`DocumentImporter` 或 `DocumentExporter`，并由宿主显式注册。

`engine.set_branch_style(node_ids, kind, scope="subtree")` 支持 `BranchStyle` 的 11 种语义样式，
`kind=None` 恢复原始曲线，`scope="level"` 仅修改选中节点。整次操作去重并只记录一次历史。
配置描述节点发出的分支，不隐式向祖先继承；新增主题复制其实际父主题的分支设置。
`MindMapDocument.new()` 显式为根主题配置圆角正交，新建引擎不产生额外撤销记录或修改标记。
读取旧文档时不回填该配置；没有分支配置的节点继续使用原始曲线。
核心提供不可变的 `PathCommand`、`BranchPath`，布局结果同时兼容原有四点 `Curve`。
