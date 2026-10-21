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
仅统计节点数时使用 `engine.node_count`，它包括折叠节点且不建立文档副本。
查询单个父级使用 `engine.parent_id(node_id)`，根返回 `None`，无效标识报错；无需重建整图父级表。

事务内部采用实体首次写入复制和变更日志，提交只校验受影响实体及结构关系。
撤销、重做和失败回滚均维护父级与资源引用索引，不再为普通编辑复制整图。
完整文档的输入、替换和输出仍执行完整校验；公共快照的隔离语义保持不变。

`engine.subscribe(callback)` 返回幂等取消订阅函数。普通修改、撤销和重做事件包含
`event.changes`，读模型可调用 `event.changes.apply(model, forward=event.forward)`
合并变化；撤销方向为 `False`。`reset` 事件重新装载文档，`saved` 事件只同步保存标记。
每个订阅者获得独立的变化实体，修改事件不会污染引擎、历史或其他监听器。
监听器中的再次编辑按修订号排队通知；不要为每次局部事件读取完整 `engine.document`。

第三方扩展实现 `LayoutAlgorithm`、`DocumentImporter` 或 `DocumentExporter`，并由宿主显式注册。
`LayoutRegistry.register(algorithm, copy_inputs=True)` 默认复制文档和测量表以隔离扩展。
受控且完全只读的实现可显式设置 `copy_inputs=False`；此时实现不得修改或保留可变输入，输入与输出的公共校验仍执行。

`engine.set_branch_style(node_ids, kind, scope="subtree")` 支持 `BranchStyle` 的 11 种语义样式，
`kind=None` 恢复原始曲线，`scope="level"` 仅修改选中节点。整次操作去重并只记录一次历史。
配置描述节点发出的分支，不隐式向祖先继承；新增主题复制其实际父主题的分支设置。
`MindMapDocument.new()` 显式为根主题配置圆角正交，新建引擎不产生额外撤销记录或修改标记。
读取旧文档时不回填该配置；没有分支配置的节点继续使用原始曲线。
核心提供不可变的 `PathCommand`、`BranchPath`，布局结果同时兼容原有四点 `Curve`。
