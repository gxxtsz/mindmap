"""面板消费的宿主协议；采用结构化类型，不导入或创建交互画布。

协议归使用它的面板包所有。普通 QWidget 或 QObject 宿主只要提供这些公开成员，
就可以接入同一套面板；无需继承 MindMapWidget，也不要求安装画布或工作区包。
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from mindmap_core import BranchStyle, LayoutRegistry, MindMapDocument, MindMapEngine
from PySide6.QtCore import SignalInstance


class PanelHost(Protocol):
    """面板与宿主的最小协作接口，所有调用及信号处理均发生在 Qt GUI 线程。

    engine 负责正式修改、事务及撤销历史；layout_registry 用于查询布局能力。
    document_view 和 parent_index 是随提交维护的借用视图，面板只能读取，不能修改
    或跨文档替换长期缓存。编辑候选仍通过 engine.document 获取隔离副本，并在
    validate_document 成功后使用正式引擎操作提交，禁止直接写入借用视图。

    document_changing 在正式视图变化和选择重建前发出，document_changed 在完成后
    发出；saved 事件只刷新保存状态。其余信号分别通知选择、只读、编辑、分支范围
    和滚轮偏好。edits_flush_requested 请求面板提交有效输入，不能强制提交输入法候选。
    """

    engine: MindMapEngine
    layout_registry: LayoutRegistry
    editable: bool
    selection_changed: SignalInstance
    document_changing: SignalInstance
    document_changed: SignalInstance
    editable_changed: SignalInstance
    editing_changed: SignalInstance
    branch_scope_changed: SignalInstance
    wheel_mode_changed: SignalInstance
    edits_flush_requested: SignalInstance
    error_occurred: SignalInstance

    @property
    def document_view(self) -> MindMapDocument:
        """返回当前借用文档，不建立整图副本；仅保证当前事件处理期间的只读使用。"""
        ...

    @property
    def parent_index(self) -> Mapping[str, str]:
        """返回节点到直接父节点的只读映射；根节点没有父条目。"""
        ...

    @property
    def is_editing(self) -> bool:
        """返回是否存在原地文字草稿；面板据此禁用可能冲突的文档修改。"""
        ...

    @property
    def branch_scope(self) -> str:
        """返回当前局部分支范围：subtree 为整个子树，level 为仅本级。"""
        ...

    @property
    def wheel_mode(self) -> str:
        """返回独立于文档及历史的滚轮偏好，取值为 scroll 或 zoom。"""
        ...

    def selected_node_ids(self) -> list[str]:
        """返回按树序排列的当前选择；调用不得改变文档、视口或历史。"""
        ...

    def reveal_nodes(self, node_ids: Sequence[str]) -> None:
        """提交有效输入并定位节点，必要的祖先展开合并为一次事务。

        只读模式需要展开时、标识无效或布局拒绝时抛出异常，由面板统一报告错误并
        恢复选择显示。宿主负责聚焦范围退出和视口定位，面板不访问场景及图元。
        """
        ...

    def validate_document(self, document: MindMapDocument) -> None:
        """隔离校验完整候选的布局和资源；失败抛出异常，正式文档及历史保持原状。"""
        ...

    def branch_kinds(self, node_ids: Sequence[str], scope: str = "subtree") -> set[BranchStyle | None]:
        """返回指定范围的有效分支样式集合；集合多项表示混合值，查询不得写入文档。"""
        ...

    def set_branch_scope(self, scope: str) -> None:
        """修改局部分支范围并通知订阅者；这属于界面状态，不产生撤销记录。"""
        ...

    def sync_branch_actions(self) -> None:
        """属性提交后同步画布动作展示；仅更新界面，不再次提交引擎操作。"""
        ...

    def set_wheel_mode(self, mode: str) -> None:
        """修改滚轮偏好，实际变化才发送信号；非法值抛出异常且保留原状态。"""
        ...

    def call(self, callback: Callable[[], Any]) -> Any:
        """执行界面动作，将业务或读写失败交给 error_occurred，避免面板自行弹错。"""
        ...
