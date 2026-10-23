# mindmap-core

## 独立子导图

`MindMapDocument.submaps` 使用扁平页面表，`Node.submap_id` 关联独立子导图；
节点与图片资源保存在整份文档中，页面只保存入口、根节点及独立布局、主题。
主导图保持既有接口，页面标识使用 `None` 表示主导图。

`engine.create_submap(node_id)` 仅允许尚无普通子节点及子导图的叶子节点，返回页面 ID。
`engine.remove_submap(node_id)` 保留入口，删除其拥有的全部页面；两种操作均支持
统一事务和整体撤销。入口与根节点名称由 `update_node` 在一个事务内沿别名链同步。
入口不能再接普通子节点，新增、粘贴和移动统一校验；每页根节点均禁止删除或移动。

`engine.page_document(page_id)` 返回隔离的单页布局读模型，`node_page_id(node_id)`
返回页内归属，`node_path(node_id)` 返回包含页面和节点 ID 的完整可点击路径。
`set_layout` 与 `set_theme` 接受可选关键字参数 `page_id`，省略时仍修改主导图。
`engine.document` 和保存快照始终包含全部页面，历史变化集的 `page_id` 表示操作所属页。
普通分支遍历不跨页，完整复制与删除通过迭代的 `owned_descendants` 遍历所有附属页面。

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

`LayoutResult.nodes` 和 `LayoutResult.branches` 的公共类型为只读 `Mapping`，
同时兼容完整布局字典和增量布局的惰性映射。调用方只查询、遍历，不依赖 `.copy()`
或条目赋值；需要独立可修改的全量结果时使用 `dict(result.nodes)` 和
`dict(result.branches)`，转换会显式枚举全部几何。

## 受控试算与后台保存

`preview_operation()` 为同步上下文接口，返回借用的候选文档和只读变化节点集合。
只允许通过引擎方法执行候选操作，不得直接修改候选、保留引用或在作用域内处理事件循环。
正常及异常退出都回滚；试算不调用前置钩子，不产生历史或事件。已有事务内禁止进入。
正式提交必须重新执行操作和前置钩子，不能将试算视为已经提交。

`capture_save_snapshot() -> SaveSnapshot` 捕获稳定只读版本和不透明保存凭据；
仅复制实体映射及配置，未变化实体依靠写时复制保持稳定，不执行全图实体深拷贝。
冻结的凭据不等于递归冻结文档，编码器不得修改 `snapshot.document`；需要可修改副本时使用 `engine.document`。
后台只接收 document。实际写入成功后，在引擎线程调用 `mark_saved_snapshot(snapshot)`；
返回 True 表示标记该历史版本，后续编辑仍保持未保存。其他引擎或替换文档前的凭据返回 False，
不发送保存事件；错误类型抛出 TypeError。两项操作均禁止在活动事务内调用。
