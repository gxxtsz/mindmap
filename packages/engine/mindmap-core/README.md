# mindmap-core

## 精确移动事件

`ChangeEvent.changes.moves` 为默认空元组的移动记录，元素是不可变的 `NodeMove`：
`node_id` 标识真正移动的分支根，`before`、`after` 分别保存事务前后的 `NodePlacement`。
位置快照包括 `node_text`、`parent_id`、`parent_text`、`index` 和 `direction`，其中同级
序号从1开始，方向为解析自动分配及父分支继承后的有效 `left`、`right` 或 `down`。

这些记录从事务中已登记的父引用变更生成，不根据兄弟节点最终顺序猜测移动对象。
多选祖先及其后代时只记录真正移动的独立根，普通兄弟和随行后代不额外记录。
被移动节点自身可能没有实体字段变化，因此不能用 `ChangeEvent.node_ids` 代替移动记录。
节点及父节点名称在事务两端固定，后续改名和删除不会污染已提交记录；构造记录不复制
整张导图，只读取受影响节点、父级子列表及所属页面的一级分支。

修改和重做的 `forward=True`，起点为 `before`、终点为 `after`；撤销的 `forward=False`，
起点改为 `after`、终点改为 `before`。通知中的快照顺序始终固定，读取者负责依据方向
描述实际发生的操作；事件订阅者依旧获得各自隔离的差量，不影响其他订阅者及历史。

位置没有净变化时不生成移动记录，试算和失败事务不发送成功事件。`ChangeSet.empty`
及 `apply()` 忽略移动元数据，不能仅靠元数据产生历史或改写文档；`ChangeSet.between()`
无法从快照还原具体移动意图，继续返回空 `moves`。新增字段位于原有三个字段之后，
兼容已有位置参数构造。移动信息只随内存撤销历史保存，不改变 `.mindmap` 文件格式。

## 独立子导图

`MindMapDocument.submaps` 使用扁平页面表，`Node.submap_id` 关联独立子导图；
节点与图片资源保存在整份文档中，页面只保存入口、根节点及独立布局、主题。

`engine.add_image_child(parent_id, data, media_type="image/png", *, width) -> str` 原子追加纯图片子节点，返回新节点 ID。
父节点必须允许普通子节点，创建后自动展开；图片字节与有限正数显示宽度由调用方传入，图像解码及像素上限由 Qt 适配层校验。
失败抛出 `MindMapError` 并回滚，成功只产生一条历史，复用资源去重、引用清理、撤销重做和保存机制。
纯图片节点使用元数据 `image_only: true`，正文为空，`Node.is_image_node` 只读返回识别结果；不能拥有详情页或子导图，可以拥有普通子节点。
缺少标记的旧图文节点保持原语义；`attach_image()` 继续为现有节点关联图片附件。文件格式仍为版本 4，不自动迁移旧节点。
上述核心接口不依赖 GUI，在所属引擎线程同步使用；ID 可跨快照保存，可写实体归文档管理，外部不直接修改借用对象。
主导图保持既有接口，页面标识使用 `None` 表示主导图。

`engine.create_submap(node_id)` 仅允许尚无普通子节点及子导图的叶子节点，返回页面 ID。
`engine.remove_submap(node_id)` 保留入口及其自身详情，删除子导图内部拥有的全部页面；两种操作均支持
统一事务和整体撤销。入口与根节点名称由 `update_node` 在一个事务内沿别名链同步。
入口不能再接普通子节点，新增、粘贴和移动统一校验；每页根节点均禁止删除或移动。

`engine.page_document(page_id)` 返回隔离的单页布局读模型，`node_page_id(node_id)`
返回页内归属，`node_path(node_id)` 返回包含页面和节点 ID 的完整可点击路径。
`set_layout` 与 `set_theme` 接受可选关键字参数 `page_id`，省略时仍修改主导图。
`engine.document` 和保存快照始终包含全部页面，历史变化集的 `page_id` 表示操作所属页。
普通分支遍历不跨页，完整复制与删除通过迭代的 `owned_descendants` 遍历所有附属页面。

## 子导图收藏接口

`favorite_submaps() -> tuple[PathEntry, ...]` 按最近收藏在前返回独立、不可变的页面根
路径项，包含页面 ID、根节点 ID 和实时名称。通过 `node_path(entry.node_id)` 的
`page_root` 项可以得到完整页面层级路径，查询不建立整份文档副本或修改文件。
`is_submap_favorite(page_id) -> bool` 查询当前提交状态，无效目标、主导图和详情页返回 False。

`set_submap_favorite(page_id, favorite) -> None` 仅接受有效子导图 ID 和布尔状态，
无效参数抛出中文 `MindMapError`，失败不修改文档。实际变化经引擎事务、前置钩子及
统一历史提交，重复设置不改变排序或产生事件；GUI 的只读与草稿约束由宿主检查。
收藏存入 `document.metadata["submap_favorites"]` 的有序页面 ID 列表，空列表省略字段。
缺失字段视为空，只读查询忽略旧程序删除页面后遗留的失效引用及不认识的元数据值。

页面或祖先分支删除时，收藏清理合并到原删除事务，撤销同时恢复页面和收藏顺序。
完整保存快照及另存为保留收藏；复制粘贴分支不会把原文件收藏关系传给新页面。
收藏只引用稳定页面 ID，名称、路径和页面归属在查询时解析，不存储重复的标题副本。

## 单层详情页

`Node.detail_page_id` 与 `MindMapDocument.detail_pages` 独立于既有子导图字段，页面记录
共用 `MindMapPage`。`create_detail_page(node_id) -> str` 允许主导图、子导图中的任意节点
创建唯一详情；`remove_detail_page(node_id)` 只删除详情内容，保留入口、普通分支与子导图。
两类页面名称都沿 ID 关联双向同步，布局和主题分别维护，禁止直接通过 `update_node` 改写页面引用。

`page_record(page_id)` 借用页面记录，`page_kind(page_id)` 返回 `main`、`submap` 或 `detail`，
`has_page(page_id)` 检查导航目标，`iter_pages()` 枚举两类附属页；借用记录禁止外部修改。
`page_document`、`page_view`、布局与主题接口接受两类页面标识，详情单页读模型也保留类别。
详情中的任何节点均不能再拥有两类附属页；操作入口、差量校验及完整导入共同执行约束。

`paste_fragment(parent_id, fragment, include_pages=True)` 默认完整复制两类页面并重建标识。
向详情页粘贴带附页的片段默认拒绝；宿主确认省略后可显式传入 `include_pages=False`，
仅复制普通分支及其属性、图片，并裁掉附属页面引用和无用资源。原片段与来源始终保持完整。
详情页同样参与写时复制、事务回滚、差量历史、图片引用计数及后台保存快照。

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
