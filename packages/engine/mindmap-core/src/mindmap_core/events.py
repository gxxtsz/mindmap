"""同步提交事件与订阅管理；监听器失败不影响已提交的文档。"""

import logging
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass

from .history import ChangeSet


@dataclass(frozen=True)
class ChangeEvent:
    """提交后通知，revision 单调递增，kind 区分修改、撤销、重做、替换及保存。

    普通提交和历史重放携带 changes；监听器可用 changes.apply(read_model,
    forward=event.forward) 更新自己的独立读模型。撤销时 forward=False。
    reset/saved 不携带差量，分别需要重新装载快照和仅同步保存标记。每个引擎
    监听器取得独立变化集，修改收到的数据不会改动历史或其他监听器的数据。
    """

    operation: str
    node_ids: tuple[str, ...]
    revision: int
    kind: str = "change"
    # 分发时每个监听器获得独立差量，避免修改历史或其他监听器的数据。
    changes: ChangeSet | None = None
    forward: bool = True


class EventDispatcher:
    """实例级事件分发器；订阅过程中取消订阅不会改变当前通知的遍历。"""

    def __init__(self):
        self._listeners: list[Callable[[ChangeEvent], None]] = []
        self._pending = deque()
        self._publishing = False

    def subscribe(self, callback: Callable[[ChangeEvent], None]) -> Callable[[], None]:
        """注册回调并返回幂等的取消订阅函数。"""
        self._listeners.append(callback)

        def unsubscribe():
            if callback in self._listeners:
                self._listeners.remove(callback)

        return unsubscribe

    def publish(self, event: ChangeEvent) -> None:
        """向提交时的订阅者快照发送通知，隔离单个监听器的异常。"""
        self._pending.append(event)
        if self._publishing:
            return
        self._publishing = True
        try:
            while self._pending:
                current = self._pending.popleft()
                for callback in tuple(self._listeners):
                    try:
                        callback(deepcopy(current) if current.changes is not None else current)
                    except Exception:
                        logging.getLogger(__name__).exception("文档监听器执行失败")
        finally:
            self._publishing = False
