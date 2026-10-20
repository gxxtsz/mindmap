"""独立事务协调器：嵌套失败会污染最外层事务，禁止部分成功提交。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass

from .errors import MindMapError
from .models import MindMapDocument


@dataclass
class DocumentState:
    """内部文档持有者；回滚替换文档后，编辑服务仍引用同一个持有者。"""

    document: MindMapDocument


class TransactionManager:
    """只在最外层保留临时恢复快照，完成清理和校验后统一提交。"""

    def __init__(
        self,
        state: DocumentState,
        finalize: Callable[[], None],
        commit: Callable[[MindMapDocument, str], None],
    ):
        self.state = state
        self.finalize = finalize
        self.commit = commit
        self.depth = 0
        self.aborted = False

    def abort(self) -> None:
        """操作钩子或禁止操作失败时标记事务，即使宿主捕获异常也不能提交。"""
        if self.depth:
            self.aborted = True

    @contextmanager
    def transaction(self, label: str) -> Iterator[None]:
        """合并嵌套操作；异常路径恢复最外层入口状态，正常路径只提交一次。"""
        outer = self.depth == 0
        before = deepcopy(self.state.document) if outer else None
        if outer:
            self.aborted = False
        self.depth += 1
        try:
            yield
            if outer:
                if self.aborted:
                    raise MindMapError("事务包含失败操作，已整体回滚")
                self.finalize()
        except Exception:
            self.aborted = True
            if outer:
                self.state.document = before
            raise
        finally:
            self.depth -= 1
        if outer:
            self.commit(before, label)
