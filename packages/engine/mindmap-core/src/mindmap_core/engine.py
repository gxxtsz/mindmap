"""拥有文档状态的编辑门面；使用组合协调操作、事务、历史和事件。"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from copy import copy, deepcopy
from dataclasses import dataclass, field

from .branch_styles import BranchStyle
from .config import LayoutConfig
from .errors import MindMapError, OperationRejected
from .events import ChangeEvent, EventDispatcher
from .history import HistoryManager
from .models import MindMapDocument, Node, Theme
from .operations import DocumentEditor
from .transactions import DocumentState, TransactionManager


@dataclass(frozen=True, slots=True)
class SaveSnapshot:
    """引擎生成的稳定保存版本，供受信任的只读编码器使用。

    document 与当前引擎共享未变化的实体，后续正式编辑采用写时复制，因此不会
    改写此版本。调用方不得修改其中的节点、资源或嵌套字段；需要可修改副本时应
    使用 engine.document。冻结外壳不代表文档被递归冻结。捕获与完成通知均应在
    引擎所属线程调用，后台进程仅接收 document，不接收此凭据或活跃引擎。

    其余字段属于不透明保存凭据，调用方不得构造、替换或解释；只能将原对象交回
    生成它的引擎。凭据不写入导图文件，也不改变公开文档快照的深拷贝语义。
    """

    document: MindMapDocument
    _owner: object = field(repr=False)
    _generation: int = field(repr=False)
    _token: str = field(repr=False)


class MindMapEngine:
    """所有修改的统一入口，读取返回独立快照。

    接口同步执行；GUI 宿主负责将调用切回 GUI 线程。before_operation 收到参数副本，
    返回 False 或抛出异常会取消操作；嵌套失败即使被捕获，最外层事务仍整体回滚。
    """

    def __init__(self, document: MindMapDocument | None = None, *, history_limit: int = 200):
        candidate = deepcopy(document if document is not None else MindMapDocument.new())
        candidate.validate()
        self._state = DocumentState(candidate)
        self._editor = DocumentEditor(self._state)
        self._history = HistoryManager(history_limit)
        self._events = EventDispatcher()
        self._transactions = TransactionManager(self._state, self._finalize, self._commit)
        self.before_operation: Callable[[str, dict], bool] | None = None
        self.revision = 0
        self.document_generation = 0
        self._previewing = False
        self._snapshot_owner = object()

    @property
    def document(self) -> MindMapDocument:
        """返回可自由读取、序列化的快照，修改它不会绕过引擎历史。"""
        return deepcopy(self._state.document)

    @property
    def root_id(self) -> str:
        return self._state.document.root_id

    @property
    def node_count(self) -> int:
        """返回当前节点总数，包括折叠节点；查询不建立文档快照。

        供状态栏等只需要统计值的宿主使用，避免为读取一个整数复制整张导图。
        返回值不持有内部文档引用，不能绕过事务、历史或修改通知。
        """
        return len(self._state.document.nodes)

    @property
    def history_limit(self) -> int:
        return self._history.limit

    @property
    def can_undo(self) -> bool:
        return self._history.can_undo

    @property
    def can_redo(self) -> bool:
        return self._history.can_redo

    @property
    def is_dirty(self) -> bool:
        return self._history.is_dirty

    @property
    def undo_label(self) -> str:
        return self._history.undo_label

    def get_node(self, node_id: str) -> Node:
        """按标识查询节点快照；无效标识使用中文异常报告。"""
        return deepcopy(self._editor.node(node_id))

    def parent_id(self, node_id: str) -> str | None:
        """返回父标识，根返回 None；不建立整图索引或快照，无效节点仍明确报错。"""
        self._editor.node(node_id)
        return self._state.parents.get(node_id)

    def subscribe(self, callback: Callable[[ChangeEvent], None]) -> Callable[[], None]:
        """订阅已提交状态并返回取消函数，监听器异常不会撤销提交。

        变化集只包含变化实体，可按事件方向更新独立读模型；不要为每次通知
        再读取 document。重入产生的新事件排队分发，所有订阅者按修订号接收。
        """
        return self._events.subscribe(callback)

    def _notify(self, operation: str, ids=(), kind="change", *, changes=None, forward=True) -> None:
        self.revision += 1
        self._events.publish(ChangeEvent(operation, tuple(ids), self.revision, kind, changes, forward))

    def _guard(self, name: str, parameters: dict) -> None:
        if self._previewing:
            return
        try:
            if self.before_operation and self.before_operation(name, deepcopy(parameters)) is False:
                raise OperationRejected(f"操作已被前置钩子取消：{name}")
        except Exception:
            self._transactions.abort()
            raise

    @contextmanager
    def transaction(self, label: str = "批量操作") -> Iterator[None]:
        """将多次修改合并为一条历史；任一嵌套操作失败则恢复事务入口状态。"""
        with self._transactions.transaction(label):
            yield

    @contextmanager
    def preview_operation(self) -> Iterator[tuple[MindMapDocument, Collection[str]]]:
        """同步试算引擎操作，退出时始终回滚；禁止嵌套进入已有事务。

        上下文返回候选文档和随操作更新的只读变化节点集合。修改只能通过当前
        引擎的编辑方法执行，禁止直接改写候选文档。候选只允许在作用域内读取，
        不得保存引用、传给不受信任扩展、处理事件循环或跨线程使用。

        操作仍执行结构维护及提交校验，但不调用 before_operation、不记录历史、
        不发事件；正常退出和异常退出都恢复原文档及索引。嵌套进入、撤销、重做、
        替换文档及保存标记等冲突操作抛出 MindMapError。正式提交仍须重新执行
        操作及前置钩子，试算成功不代表获得正式修改授权。
        """
        self._require_idle("事务期间不能执行候选试算")
        self._previewing = True
        try:
            with self._transactions.transaction("布局试算", rollback_only=True):
                yield self._state.document, self._state.journal.nodes.keys()
        finally:
            self._previewing = False

    def _execute(self, name: str, action: Callable, **parameters):
        self._guard(name, parameters)
        with self.transaction(name):
            return action(**parameters)

    def _finalize(self, journal) -> None:
        # 结构由受控编辑器维护；变化实体复用导入字段校验，资源按引用差量清理。
        from .validation import validate_changes

        validate_changes(self._state, journal)

    def _commit(self, delta, label: str) -> None:
        if not delta.empty:
            self._state.update_indexes(delta, parents=False)
            self._history.record(delta, label)
            self._notify(label, delta.node_ids, changes=delta)

    def _require_idle(self, message: str) -> None:
        if self._transactions.depth:
            self._transactions.abort()
            raise MindMapError(message)

    def undo(self) -> bool:
        """撤销一次提交；空历史返回 False，事务期间禁止移动历史游标。"""
        self._require_idle("事务期间不能撤销")
        if not self.can_undo:
            return False
        self._guard("undo", {})
        entry = self._history.undo(self._state.document)
        self._state.update_indexes(entry.delta, forward=False)
        self._notify(entry.label, entry.delta.node_ids, "undo", changes=entry.delta, forward=False)
        return True

    def redo(self) -> bool:
        """重做一次提交，空重做分支返回 False。"""
        self._require_idle("事务期间不能重做")
        if not self.can_redo:
            return False
        self._guard("redo", {})
        entry = self._history.redo(self._state.document)
        self._state.update_indexes(entry.delta)
        self._notify(entry.label, entry.delta.node_ids, "redo", changes=entry.delta)
        return True

    def mark_saved(self) -> None:
        """文件完整写入后由宿主调用；撤销到此位置时恢复未修改状态。"""
        self._require_idle("事务提交前不能建立保存标记")
        self._history.mark_saved()
        self._notify("保存", kind="saved")

    def capture_save_snapshot(self) -> SaveSnapshot:
        """捕获正式版本及保存凭据，不改变历史或修改状态。

        引擎写入已统一采用实体写时复制，因此此处只复制字典和文档配置即可
        固定节点版本。后台编码器只能读取；公共 document 仍返回完整深拷贝。
        必须在引擎所属线程且没有活动事务时捕获，否则抛出 MindMapError。
        返回的 document 仅供受信任编码器读取，禁止修改；后台写入成功后将整个
        SaveSnapshot 交回 mark_saved_snapshot。写入失败不得标记保存成功。
        """
        self._require_idle("事务提交前不能取得保存快照")
        source = self._state.document
        document = copy(source)
        document.nodes, document.assets = source.nodes.copy(), source.assets.copy()
        document.layout, document.theme = deepcopy(source.layout), deepcopy(source.theme)
        document.metadata = deepcopy(source.metadata)
        return SaveSnapshot(document, self._snapshot_owner, self.document_generation, self._history._state)

    def mark_saved_snapshot(self, snapshot: SaveSnapshot) -> bool:
        """标记实际写入的版本，期间的新编辑仍保持未保存。

        snapshot 必须由 capture_save_snapshot 取得；错误类型抛出 TypeError。
        其他引擎或替换文档之前的凭据返回 False，不改变保存位置、不发送事件。
        有效凭据返回 True 并发出 saved 事件；它标记捕获时的历史状态，因此只有
        当前历史游标与该版本一致时文档才变为干净。活动事务内禁止调用。
        """
        if not isinstance(snapshot, SaveSnapshot):
            raise TypeError("保存凭据必须为 SaveSnapshot")
        self._require_idle("事务提交前不能建立保存标记")
        if snapshot._owner is not self._snapshot_owner or snapshot._generation != self.document_generation:
            return False
        self._history.mark_saved(snapshot._token)
        self._notify("保存", kind="saved")
        return True

    def replace_document(self, document: MindMapDocument) -> None:
        """校验新快照后原子替换并清空历史；算法是否可用由宿主布局服务检查。"""
        self._require_idle("事务期间不能替换文档")
        candidate = deepcopy(document)
        candidate.validate()
        self._guard("replace_document", {})
        self._state.reset(candidate)
        self._history.reset()
        self.document_generation += 1
        self._notify("打开文档", candidate.nodes, "reset")

    def add_child(self, parent_id: str, text: str = "新节点", *, index: int | None = None) -> str:
        """添加子节点，返回新标识；index 省略时追加并展开父节点。"""
        return self._execute("add_child", self._editor.add_child, parent_id=parent_id, text=text, index=index)

    def add_sibling(self, node_id: str, text: str = "新节点", *, before: bool = False) -> str:
        """添加同级节点，保持方向提示；根节点不允许拥有同级。"""
        return self._execute(
            "add_sibling", self._editor.add_sibling, node_id=node_id, text=text, before=before
        )

    def insert_parent(self, node_id: str, text: str = "父节点") -> str:
        """插入一级父节点，保留原节点及其后代标识。"""
        return self._execute("insert_parent", self._editor.insert_parent, node_id=node_id, text=text)

    def update_node(self, node_id: str, **patch) -> None:
        """更新节点内容、样式或元数据；标识和子节点列表必须由结构操作修改。"""

        def action(node_id, patch):
            self._editor.update_node(node_id, **patch)

        self._execute("update_node", action, node_id=node_id, patch=patch)

    def set_branch_style(self, node_ids, kind: BranchStyle | str | None, *, scope: str = "subtree") -> None:
        """批量设置主题发出的连接线，一次调用只产生一条可撤销记录。

        subtree 更新所有后代（含叶节点及折叠节点），level 只更新指定主题；
        None 恢复原始曲线。与普通更新共用前置钩子、事务回滚和保存标记。
        """
        self._execute(
            "set_branch_style", self._editor.set_branch_style, node_ids=list(node_ids), kind=kind, scope=scope
        )

    def remove_nodes(self, node_ids) -> None:
        """删除选中子树；重复或被选中祖先覆盖的条目只处理一次。"""
        self._execute("remove_nodes", self._editor.remove_nodes, node_ids=list(node_ids))

    def move_nodes(self, node_ids, target_id: str, position: str = "inside") -> None:
        """按先序移动子树；位置支持 inside、before、after，禁止形成循环。"""
        self._execute(
            "move_nodes",
            self._editor.move_nodes,
            node_ids=list(node_ids),
            target_id=target_id,
            position=position,
        )

    def move_up(self, node_id: str, *, down: bool = False) -> None:
        """调整同级顺序；内部结构移动仍通过引擎，保留嵌套钩子和失败语义。"""

        def action(node_id, down):
            self._editor.node(node_id)
            parent = self._state.parents.get(node_id)
            if parent is None:
                raise MindMapError("根节点不能调整同级顺序")
            siblings = self._editor.node(parent).children
            index = siblings.index(node_id) + (1 if down else -1)
            if 0 <= index < len(siblings):
                self.move_nodes([node_id], siblings[index], "after" if down else "before")

        self._execute("move_up", action, node_id=node_id, down=down)

    def set_expanded(self, node_id: str, expanded: bool, *, recursive: bool = False) -> None:
        """设置折叠状态；递归修改整棵子树也只产生一条历史。"""
        self._execute(
            "set_expanded", self._editor.set_expanded, node_id=node_id, expanded=expanded, recursive=recursive
        )

    def set_layout(self, config: LayoutConfig) -> None:
        """保存独立布局配置；核心仅校验结构，宿主在界面使用前校验具体算法。"""

        def action(config):
            self._state.set_setting("layout", config)

        self._execute("set_layout", action, config=config)

    def set_theme(self, theme: Theme) -> None:
        """保存主题副本，避免多个文档共享可变主题。"""

        def action(theme):
            self._state.set_setting("theme", theme)

        self._execute("set_theme", action, theme=theme)

    def attach_image(self, node_id: str, data: bytes, media_type: str = "image/png", *, width=160) -> str:
        """导入并关联图片；相同字节按内容哈希去重，关联和清理一并进入历史。"""
        return self._execute(
            "attach_image",
            self._editor.attach_image,
            node_id=node_id,
            data=data,
            media_type=media_type,
            width=width,
        )

    def copy_fragment(self, node_ids) -> MindMapDocument:
        """建立包含属性、资源和布局配置的独立剪贴板文档，不修改原文档。"""
        return self._editor.copy_fragment(node_ids)

    def paste_fragment(self, parent_id: str, fragment: MindMapDocument) -> list[str]:
        """验证并插入片段，重建全部节点标识，并复用相同图片资源。"""
        return self._execute(
            "paste_fragment", self._editor.paste_fragment, parent_id=parent_id, fragment=fragment
        )

    def copy_nodes(self, node_ids, parent_id: str) -> list[str]:
        """在一个事务中复制并粘贴子树，保留两个步骤的失败与钩子约束。"""

        def action(node_ids, parent_id):
            return self.paste_fragment(parent_id, self.copy_fragment(node_ids))

        return self._execute("copy_nodes", action, node_ids=list(node_ids), parent_id=parent_id)
