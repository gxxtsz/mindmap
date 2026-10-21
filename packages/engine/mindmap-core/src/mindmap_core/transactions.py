"""独立事务协调器：嵌套失败会污染最外层事务，禁止部分成功提交。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from .errors import MindMapError
from .journal import DocumentState, TransactionJournal


class TransactionManager:
    """最外层创建实体日志，局部清理和校验成功后统一提交差量。"""

    def __init__(
        self,
        state: DocumentState,
        finalize: Callable,
        commit: Callable,
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
    def transaction(self, label: str, *, rollback_only=False) -> Iterator[None]:
        """合并嵌套操作；异常恢复登记实体，通知前结束日志生命周期以允许后续编辑。"""
        outer = self.depth == 0
        if outer:
            self.aborted = False
            self.state.journal = TransactionJournal(self.state)
        journal = self.state.journal
        self.depth += 1
        try:
            yield
            if outer:
                if self.aborted:
                    raise MindMapError("事务包含失败操作，已整体回滚")
                self.finalize(journal)
                if rollback_only:
                    journal.rollback()
                else:
                    changes = journal.changes()
        except Exception:
            self.aborted = True
            if outer:
                journal.rollback()
            raise
        finally:
            self.depth -= 1
            if outer:
                self.state.journal = None
        if outer and not rollback_only:
            self.commit(changes, label)
