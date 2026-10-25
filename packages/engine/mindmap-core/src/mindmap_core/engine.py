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
from .models import MindMapDocument, Node, PathEntry, Theme
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
        self._event_source_id: str | None = None

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

    def node_page_id(self, node_id: str) -> str | None:
        """查询节点所在区域或页面，主导图返回 None，灵感区返回 inspiration_id。

        仅沿普通父链读取运行时索引，不跨附属页面入口，不复制或修改文档。
        无效节点抛出中文 MindMapError；同步调用，返回标识可跨快照使用。
        """
        self._editor.node(node_id)
        return self._state.page_id(node_id)

    def page_document(self, page_id: str | None = None, *,
                      inspiration_root_id: str | None = None) -> MindMapDocument:
        """返回独立的区域快照，供布局、渲染、面板和图形导出使用。

        page_id 省略或为 None 时读取主导图；灵感区标识返回可为空的
        MindMapForestView，以 root_ids 枚举游离树，附属页面继续返回单根快照。
        结果归调用方所有，修改不会写回引擎，不能作为整份文档保存；无效页面
        抛出 MindMapError。查询不产生历史或修改标记，仅在引擎所属线程同步调用。
        inspiration_root_id 指定灵感区中有效的游离根时，只复制这一棵普通树，
        展示根坐标归零，实际总览坐标保持不变；省略仍返回全部游离树。
        该参数用于其他页面或指定非游离根时抛出 MindMapError，结果不借用引擎实体。
        """
        try:
            return deepcopy(self._state.document.page_view(page_id, inspiration_root_id=inspiration_root_id))
        except KeyError as exc:
            raise MindMapError("导图页面不存在") from exc

    def node_path(self, node_id: str) -> list[PathEntry]:
        """迭代构造全局祖先路径，入口与子图根合并，保留每一级页面根的跳转项。

        空页面的根也可能拥有子导图，此时相邻页面根虽然同名，仍分别保留，
        避免深层根节点链失去返回入口。所有关联均使用 ID，不能按标题去重。
        """
        self._editor.node(node_id)
        result = []
        key = node_id
        while True:
            page_id = self._state.page_id(key)
            page = self._state.document.page_record(page_id)
            if page_id == self.inspiration_id:
                local = []
                while True:
                    local.append(PathEntry(page_id, key, self._editor.node(key).text))
                    if key not in self._state.parents:
                        break
                    key = self._state.parents[key]
                return [PathEntry(page_id, None, "灵感区", True), *reversed(local), *result]
            local = []
            while True:
                node = self._editor.node(key)
                local.append(PathEntry(page_id, key, node.text, key == page.root_id))
                if key == page.root_id:
                    break
                key = self._state.parents[key]
            result[0:0] = list(reversed(local))
            if page_id is None:
                break
            owner = page.owner_id
            parent_page = self._state.page(owner)
            key = (owner if self._state.page_id(owner) == self.inspiration_id
                   or owner == parent_page.root_id else self._state.parents[owner])
        return result

    @property
    def inspiration_id(self) -> str:
        """读取当前文档灵感区的稳定页面标识，不复制文档或产生历史。"""
        return self._state.document.inspiration.id

    def inspiration_items(self) -> tuple[tuple[str, str, int], ...]:
        """按游离根顺序返回侧栏摘要，每项为（根标识、原始标题、普通后代数量）。

        不包含根本身或附属页面内部节点，不复制实体及图片字节，不借用可变容器。
        空区返回空元组；查询不修改文档、历史或保存标记。仅在引擎所属线程读取，
        返回的不可变快照可由界面保存，后续编辑不会改变其中的旧标题和计数。
        """
        document = self._state.document
        return tuple((root, document.nodes[root].text, len(document.descendants(root)) - 1)
                     for root in document.inspiration.root_ids)

    def add_inspiration(self, text="新想法", *, position) -> str:
        """创建游离根并返回稳定节点 ID，普通后代及附属页面能力沿用普通节点。

        position 是两个有限数值组成的逻辑像素坐标；非法文本或坐标抛出
        MindMapError，失败完整回滚。成功只提交一次历史，不依赖 Qt；只能在
        引擎所属线程同步调用，返回标识可跨快照保存，实体仍归文档管理。
        """
        return self._execute("add_inspiration", self._editor.add_inspiration,
                             text=text, position=position)

    def set_inspiration_positions(self, positions) -> None:
        """原子调整游离根位置；映射键为有效根 ID，值为有限二维逻辑坐标。

        不调整普通后代，不改变根序；相同坐标不产生历史。失效根、子节点或非法
        坐标抛出 MindMapError 并回滚；一次多根调整只产生一条历史。同步接口，
        不持有传入映射，也不允许调用方直接改写文档借用记录。
        """
        self._execute("set_inspiration_positions", self._editor.set_inspiration_positions,
                      positions=positions)

    def move_to_inspiration(self, node_ids, *, positions) -> list[str]:
        """把同页选中分支无损移出并返回去重后的分支根，详情页内部不接受移动。

        positions 必须恰好覆盖排除祖先重复选择后的每个根；普通页根禁止移出。
        节点、图片及全部附属页标识保持不变，坐标与归属共同进入一次历史。
        非法目标或坐标抛出 MindMapError，完整回滚；仅限引擎所属线程同步调用。
        """
        return self._execute("move_to_inspiration", self._editor.move_to_inspiration,
                             node_ids=list(node_ids), positions=positions)

    def attach_inspirations(self, node_ids, parent_id) -> None:
        """把灵感区分支追加到主导图或子导图父主题，保留所有内容及稳定标识。

        目标必须能接普通子节点，不能位于源分支拥有的普通后代或附属页面中。
        非法来源、页面根约束或归属循环抛出 MindMapError，整个操作回滚。
        成功只提交一次历史，父主题自动展开；同步调用，不依赖 GUI 或剪贴板。
        """
        self._execute("attach_inspirations", self._editor.attach_inspirations,
                      node_ids=list(node_ids), parent_id=parent_id)

    def group_inspirations(self, node_ids, text="新主题", *, position) -> str:
        """新建共同游离父主题，按原树序组合选中分支，返回新父主题 ID。

        来源必须都在灵感区；重复及祖先后代选择只保留最上层分支。position 为
        新父主题的有限二维逻辑坐标，失败抛出 MindMapError 并完整回滚。
        创建与组合只产生一条历史，后续改名另记历史；仅限所属引擎线程同步调用。
        """
        return self._execute("group_inspirations", self._editor.group_inspirations,
                             node_ids=list(node_ids), text=text, position=position)

    def create_submap(self, node_id: str) -> str:
        """为普通叶子节点创建独立子导图并返回页面 ID，实际进入由界面负责。

        node_id 必须存在，且不为纯图片、详情页内节点，也不能已有普通子节点或
        子导图；失败抛出 MindMapError，不留下部分修改。页面和入口共用一次历史。
        接口不依赖 Qt，仅在引擎所属线程同步调用；标识可跨快照保存，页面由文档管理。
        """
        return self._execute("create_submap", self._editor.create_submap, node_id=node_id)

    def remove_submap(self, node_id: str) -> None:
        """删除入口拥有的全部子导图，一次撤销可完整恢复，入口节点保持原位。"""
        self._execute("remove_submap", self._editor.remove_submap, node_id=node_id)

    def favorite_submaps(self) -> tuple[PathEntry, ...]:
        """按最近收藏在前的顺序返回子导图根路径项，不建立整份文档快照。

        返回元组中的 PathEntry 均为独立、不可变的页面描述，包含页面 ID、根节点
        ID 和当前名称；完整页面路径可再通过 node_path(entry.node_id) 查询。
        缺少收藏元数据时返回空元组，忽略旧程序删除页面后遗留的无效引用；
        查询不会修补文件、改变排序、发送事件或增加撤销记录。
        """
        document = self._state.document
        return tuple(PathEntry(key, document.submaps[key].root_id,
                               document.nodes[document.submaps[key].root_id].text, True)
                     for key in self._editor.favorite_submap_ids())

    def is_submap_favorite(self, page_id: str) -> bool:
        """查询指定子导图是否已收藏，不修改文档或历史。

        page_id 使用稳定页面标识；主导图、详情页、不存在的标识及非字符串输入
        均返回 False。返回值只反映当前提交状态，不包含界面尚未提交的输入。
        """
        return isinstance(page_id, str) and page_id in self._editor.favorite_submap_ids()

    def set_submap_favorite(self, page_id: str, favorite: bool) -> None:
        """设置子导图收藏状态，返回 None，实际变化纳入一次事务及统一撤销历史。

        page_id 必须是当前文档内有效的子导图 ID，favorite 必须为布尔值；主导图、
        详情页、失效标识或错误参数均抛出 MindMapError，失败时不改变任何内容。
        新收藏置于列表首位，重复设置不改变顺序、不发送修改事件。取消后再次
        收藏按新的收藏时间排序。只读、输入法及草稿提交由 GUI 宿主在调用前检查。
        """
        self._execute("set_submap_favorite", self._editor.set_submap_favorite,
                      page_id=page_id, favorite=favorite)

    def create_detail_page(self, node_id: str) -> str:
        """为主图或子导图节点创建详情页并返回页面 ID；详情页内调用会原子失败。

        node_id 必须存在且不是纯图片节点，不限制普通子节点或已有子导图，每个节点
        最多一张详情页；非法目标抛出 MindMapError，不留下部分修改。页面切换由宿主
        负责，接口不依赖 Qt，仅在引擎所属线程同步调用，返回 ID 的生命周期由文档管理。
        """
        return self._execute("create_detail_page", self._editor.create_detail_page, node_id=node_id)

    def remove_detail_page(self, node_id: str) -> None:
        """删除节点拥有的详情内容，保留原节点、普通分支与子导图，可一次撤销恢复。"""
        self._execute("remove_detail_page", self._editor.remove_detail_page, node_id=node_id)

    def subscribe(self, callback: Callable[[ChangeEvent], None]) -> Callable[[], None]:
        """订阅已提交状态并返回取消函数，监听器异常不会撤销提交。

        变化集只包含变化实体，可按事件方向更新独立读模型；不要为每次通知
        再读取 document。重入产生的新事件排队分发，所有订阅者按修订号接收。
        """
        return self._events.subscribe(callback)

    def _notify(self, operation: str, ids=(), kind="change", *, changes=None, forward=True) -> None:
        self.revision += 1
        self._events.publish(ChangeEvent(operation, tuple(ids), self.revision, kind, changes, forward,
                                        self._event_source_id))

    @contextmanager
    def event_source(self, source_id: str | None) -> Iterator[None]:
        """为同步作用域内发出的事件标记交互来源，不修改文档或撤销历史。

        source_id 是宿主自定的非空视图标识，None 表示未指定来源；非法参数抛出
        ValueError。允许嵌套，正常退出或异常退出均恢复外层来源。标识在事件创建时
        捕获，因此重入排队通知也保留实际来源；撤销使用本次触发者而非原编辑者。
        仅在引擎所属线程同步使用；异步任务应在实际提交时重新进入作用域。
        """
        if source_id is not None and (not isinstance(source_id, str) or not source_id):
            raise ValueError("事件来源必须为非空字符串或 None")
        previous = self._event_source_id
        self._event_source_id = source_id
        try:
            yield
        finally:
            self._event_source_id = previous

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
            journal = self._state.journal
            if not journal.has_origin:
                key = parameters.get("node_id") or parameters.get("parent_id") or parameters.get("target_id")
                ids = parameters.get("node_ids")
                key = key or (ids[0] if ids else None)
                journal.page_id = self._state.page_id(key) if key is not None else parameters.get("page_id")
                if key is None and name in ("add_inspiration", "set_inspiration_positions"):
                    journal.page_id = self.inspiration_id
                journal.has_origin = True
            return action(**parameters)

    def _finalize(self, journal) -> None:
        # 结构由受控编辑器维护；变化实体复用导入字段校验，资源按引用差量清理。
        from .validation import validate_changes

        # 页面删除与收藏清理共享同一条差量历史；只检查本次变化的页面，避免局部
        # 编辑扫描整份文件。撤销时元数据和页面同时恢复，收藏顺序也保持原样。
        removed = {key for key in journal.submaps if key not in self._state.document.submaps}
        if removed:
            self._editor.prune_submap_favorites(removed)
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
        document.submaps = source.submaps.copy()
        document.detail_pages = source.detail_pages.copy()
        document.layout, document.theme = deepcopy(source.layout), deepcopy(source.theme)
        document.metadata = deepcopy(source.metadata)
        document.inspiration = deepcopy(source.inspiration)
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

    def set_layout(self, config: LayoutConfig, *, page_id: str | None = None) -> None:
        """保存独立布局配置；核心仅校验结构，宿主在界面使用前校验具体算法。"""

        def action(config, page_id):
            if page_id is None:
                self._state.set_setting("layout", config)
            else:
                page = deepcopy(self._state.document.page_record(page_id))
                page.layout = deepcopy(config)
                if page_id == self.inspiration_id:
                    self._state.set_setting("inspiration", page)
                else:
                    self._state.set_page(page_id, page)

        self._execute("set_layout", action, config=config, page_id=page_id)

    def set_theme(self, theme: Theme, *, page_id: str | None = None) -> None:
        """保存主题副本，避免多个文档共享可变主题。"""

        def action(theme, page_id):
            if page_id is None:
                self._state.set_setting("theme", theme)
            else:
                page = deepcopy(self._state.document.page_record(page_id))
                page.theme = deepcopy(theme)
                if page_id == self.inspiration_id:
                    self._state.set_setting("inspiration", page)
                else:
                    self._state.set_page(page_id, page)

        self._execute("set_theme", action, theme=theme, page_id=page_id)

    def add_image_child(self, parent_id: str, data: bytes, media_type: str = "image/png", *, width: float) -> str:
        """原子追加纯图片子节点并返回新节点 ID，整个创建只产生一条历史。

        parent_id 是允许普通子节点的父节点，data 为独立图片字节，media_type 为
        图片 MIME，width 为有限正数的逻辑显示宽度；Qt 调用方须先验证可解码性，
        默认传入原图像素宽度。父节点会展开，新节点正文为空且不拥有附属页面。
        无效父节点、子导图入口、空资源或非法宽度抛出 MindMapError，失败不保留
        节点、资源或历史。资源复用和撤销清理沿用 attach_image 的内容哈希规则。
        本接口不依赖 Qt，仅在引擎所属线程同步调用；返回 ID 可跨快照保存，实体
        生命周期由文档及历史管理，外部不得持有可写实体引用。
        """
        return self._execute(
            "add_image_child", self._editor.add_image_child,
            parent_id=parent_id, data=data, media_type=media_type, width=width,
        )

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

    def paste_fragment(self, parent_id: str, fragment: MindMapDocument, *, include_pages: bool = True) -> list[str]:
        """验证并插入片段，重建节点及页面标识，并按内容哈希复用图片资源。

        include_pages 默认无损保留附属页面；详情页目标不接受这些页面，宿主可在用户
        确认后显式传入 False，只粘贴普通树和所需资源，来源及传入片段均不被修改。
        """
        return self._execute(
            "paste_fragment", self._editor.paste_fragment, parent_id=parent_id, fragment=fragment,
            include_pages=include_pages,
        )

    def copy_nodes(self, node_ids, parent_id: str) -> list[str]:
        """在一个事务中复制并粘贴子树，保留两个步骤的失败与钩子约束。"""

        def action(node_ids, parent_id):
            return self.paste_fragment(parent_id, self.copy_fragment(node_ids))

        return self._execute("copy_nodes", action, node_ids=list(node_ids), parent_id=parent_id)
