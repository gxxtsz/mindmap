"""同步提交事件与订阅管理；监听器失败不影响已提交的文档。"""

import logging
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ChangeEvent:
    """提交后通知；revision 单调递增，kind 区分普通修改、撤销、重做及文档替换。"""

    operation: str
    node_ids: tuple[str, ...]
    revision: int
    kind: str = "change"


class EventDispatcher:
    """实例级事件分发器；订阅过程中取消订阅不会改变当前通知的遍历。"""

    def __init__(self):
        self._listeners: list[Callable[[ChangeEvent], None]] = []

    def subscribe(self, callback: Callable[[ChangeEvent], None]) -> Callable[[], None]:
        """注册回调并返回幂等的取消订阅函数。"""
        self._listeners.append(callback)

        def unsubscribe():
            if callback in self._listeners:
                self._listeners.remove(callback)

        return unsubscribe

    def publish(self, event: ChangeEvent) -> None:
        """向提交时的订阅者快照发送通知，隔离单个监听器的异常。"""
        for callback in tuple(self._listeners):
            try:
                callback(event)
            except Exception:
                logging.getLogger(__name__).exception("文档监听器执行失败")
