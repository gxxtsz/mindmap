"""把后台 Git 检查、共享文档和顶栏同步菜单连接起来。"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mindmap_core.formats import DirectoryVersionConflict
from mindmap_desktop_api.qt import TitleBarItemSpec
from PySide6.QtCore import QObject, QSize, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QToolButton

from .git_repository import GitRepository, GitStatus, GitSyncError, RemoteCheck, Upstream

if TYPE_CHECKING:
    from .controller import MindMapController
    from .documents import MindMapDocumentHandle

_LOCAL_INTERVAL = 2_000
_REMOTE_INTERVAL = 30_000
_CHECK, _UPDATE, _COMMIT, _SYNC = "立即检查", "更新导图", "仅提交", "提交并推送"


@dataclass
class _Task:
    """工作线程只持有 future 的输入；文档和回调始终由 GUI 线程保存和访问。"""

    future: Future
    done: Callable
    failed: Callable[[Exception], None]


_SavedAction = tuple["MindMapDocumentHandle", GitRepository, str, GitStatus, RemoteCheck | None]


class GitSyncController(QObject):
    """按活动导图显示 Git 状态。同一路径的任务串行，其他标签可继续使用。

    同一路径的写操作串行，本地和远程检查有独立计时器。慢速远程检查期间仍可
    读取本地状态；任务回到主线程后按文档和路径识别归属。
    """

    def __init__(self, window: MindMapController):
        super().__init__(window)
        self.window = window
        self.document: MindMapDocumentHandle | None = None
        self.path: Path | None = None
        self.status: GitStatus | None = None
        self.remote: RemoteCheck | None = None
        self.local_error = ""
        self._disposed = False
        self._jobs: dict[tuple[Path, str], _Task] = {}
        self._notified: dict[tuple[Path, Upstream], str] = {}
        self._waiting_save: dict[Path, _SavedAction] = {}
        self._local_again = False
        self._want_remote = False
        self._manual_check = False
        self._appearance = None
        self._last_text = ""
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mindmap-git")
        self._local_timer = self._timer(_LOCAL_INTERVAL, self._read_local)
        self._remote_timer = self._timer(_REMOTE_INTERVAL, self._request_remote)
        self._poll = self._timer(40, self._finish_jobs)

        self.button = QToolButton()
        self.button.setObjectName("mindmap_git_sync")
        self.button.setProperty("titleBarAction", True)
        self.button.setFixedHeight(30)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setIconSize(QSize(15, 15))
        self.button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = QMenu(self.button)
        self.menu.setToolTipsVisible(True)
        self.description = self.menu.addAction("正在读取 Git 状态")
        self.description.setEnabled(False)
        self.menu.addSeparator()
        self.actions: dict[str, QAction] = {}
        for label, run in ((_CHECK, lambda: self._request_remote(manual=True)),
                           (_UPDATE, lambda: self._start(_UPDATE)), (_COMMIT, lambda: self._start(_COMMIT)),
                           (_SYNC, lambda: self._start(_SYNC))):
            self.actions[label] = self.menu.addAction(label, run)
        self.menu.aboutToShow.connect(self._render)
        self.button.setMenu(self.menu)
        window.context.ui.register_title_bar_item(
            TitleBarItemSpec("mindmap.git", self.button, lambda: self.document is not None))
        window.context.ui.subscribe_theme(self._render)
        self._render()

    def _timer(self, interval, run):
        timer = QTimer(self)
        timer.setInterval(interval)
        timer.timeout.connect(run)
        return timer

    def bind(self, document: MindMapDocumentHandle | None) -> None:
        """切换当前文档或另存时重绑检查；同一共享文档的多个标签复用已有任务。"""
        if self._disposed:
            return
        path = document.path if document else None
        if document is not self.document or path != self.path:
            self.menu.close()
            self.document, self.path = document, path
            self.status = self.remote = None
            self.local_error = ""
            self._local_again = self._manual_check = False
            self._want_remote = path is not None
            self._local_timer.stop()
            self._remote_timer.stop()
            if path is not None:
                self._local_timer.start()
                self._remote_timer.start()
                self._read_local()
        self._render()

    def document_saved(self, document: MindMapDocumentHandle) -> None:
        """保存（含后台写入）结束即重新读取本地状态；合并正在进行的同类检查。"""
        if document is self.document and document.path == self.path:
            self._read_local()

    def _current(self, path: Path) -> bool:
        return not self._disposed and self.path == path and self.document is not None

    def _has(self, path: Path | None, *kinds: str) -> bool:
        return any((path, kind) in self._jobs for kind in kinds)

    def _busy(self, path: Path | None) -> bool:
        return self._has(path, "action") or path in self._waiting_save

    def _submit(self, path, kind, run, done, failed):
        key = path, kind
        self._jobs[key] = _Task(self._pool.submit(run), done, failed)
        if not self._poll.isActive():
            self._poll.start()

    def _finish_jobs(self):
        # 不给 Future 注册 Qt 回调。关闭时丢弃查询结果，已运行的短时工作有超时边界。
        for key, job in tuple(self._jobs.items()):
            if self._disposed:
                break
            if not job.future.done() or self._jobs.get(key) is not job:
                continue
            del self._jobs[key]
            try:
                result = job.future.result()
            except Exception as error:
                job.failed(error)
            else:
                job.done(result)
        for path, pending in tuple(self._waiting_save.items()):
            if not pending[0].is_writing:
                del self._waiting_save[path]
                self._wait_for_save(*pending, resumed=True)
        if not self._jobs and not self._waiting_save:
            self._poll.stop()

    def _read_local(self):
        path = self.path
        if self._disposed or path is None or self._busy(path) or self.document.git_busy:
            return
        if self._has(path, "local"):
            self._local_again = True
            return
        # 保存事务会短暂替换多个文件。等保存通知再读，不把中间文件显示成改动。
        if self.document.is_writing:
            return
        self._local_again = False
        repository = GitRepository(path)
        self._submit(path, "local", repository.status,
                     lambda state: self._receive_local(path, state),
                     lambda error: self._receive_local(path, None, error))

    def _receive_local(self, path, state, error=None):
        if not self._current(path) or self._busy(path) or self.document.git_busy:
            return
        old = self.status
        if old is None or state is None or old.upstream != state.upstream:
            self.remote = None
            if state is not None and state.upstream:
                self._want_remote = True
        self.status, self.local_error = state, str(error) if error is not None else ""
        self._render()
        self._notify_remote()
        if self._want_remote:
            self._fetch()
        if self._local_again:
            self._read_local()

    def _request_remote(self, *, manual=False):
        if self.path is None:
            return
        self._want_remote = True
        self._manual_check |= manual
        self._read_local()
        self._render()

    def _fetch(self):
        path, state = self.path, self.status
        if path is None or self._has(path, "remote", "action"):
            return
        if state is None or state.upstream is None:
            if self._manual_check:
                self.window.show_status_message(self.local_error or (state.problem if state else "正在读取 Git 状态"))
                self._manual_check = False
            self._want_remote = False
            return
        self._want_remote = False
        repository = GitRepository(path)
        self._submit(path, "remote", lambda: repository.fetch(state.upstream),
                     lambda check: self._receive_remote(path, check),
                     lambda error: self._receive_remote(path, RemoteCheck(state.upstream, error=str(error))))
        self._render()

    def _receive_remote(self, path, check: RemoteCheck):
        if not self._current(path):
            return
        if self.status is not None and check.upstream == self.status.upstream:
            self.remote = check
            if self._manual_check:
                self.window.show_status_message(f"远程状态未知：{check.error}" if check.error else "已检查远程更新")
                self._manual_check = False
        else:
            self._want_remote = True
        # 先读最新跟踪引用，再渲染本地和远程各自领先的提交数。
        self._read_local()
        self._render()

    def _notify_remote(self):
        state, check = self.status, self.remote
        if (self.path is None or state is None or check is None or check.upstream != state.upstream
                or check.error or not state.behind or state.remote_head != check.head):
            return
        key = self.path, check.upstream
        if self._notified.get(key) != check.head:
            self._notified[key] = check.head
            self.window.show_status_message(f"{self.path.name}：远程有 {state.behind} 个新提交")

    def _pending_save(self) -> bool:
        document = self.document
        return bool(document and (document.is_dirty or document.is_writing or any(
            view.canvas.is_editing or view.is_composing or view.panels.has_pending_edits for view in document.views)))

    def _render(self, *_):
        if self._disposed:
            return
        state, check, path, document = self.status, self.remote, self.path, self.document
        busy = self._busy(path) or bool(document and document.git_busy)
        checking = self._has(path, "remote")
        pending = self._pending_save()
        flags, detail = [], []
        remote_current = bool(state and check and check.head and state.upstream == check.upstream
                              and state.remote_head == check.head)
        if busy:
            flags.append("正在同步")
        if pending:
            flags.append("待保存")
        if document and document.file_state.conflict:
            flags.append("保存冲突")
            detail.append("请在「文件」菜单重新加载磁盘版本或另存")
        ignored = sum(change.code == "!!" for change in state.members) if state else 0
        if state and state.members:
            flags.append("未提交")
            detail.append(f"{len(state.members)} 个导图文件未提交")
        if ignored:
            detail.append(f"{ignored} 个导图文件被 Git 忽略，请调整忽略规则后提交")
        if state and state.ahead:
            flags.append("待推送")
            detail.append(f"{state.ahead} 个本地提交待推送")
        if state and state.behind:
            flags.append("远程有更新" if remote_current else "上次发现远程更新")
            detail.append(f"远程跟踪分支有 {state.behind} 个新提交")
            if pending or state.changes or state.ahead:
                detail.append("两端都有修改；请先在 Git 中处理，可以先仅提交导图")
        if self.local_error:
            flags.append("本地状态未知")
            detail.append(self.local_error)
        elif state and state.problem:
            flags.append(state.problem.split("，", 1)[0])
            detail.append(state.problem)
        elif path is not None and state is None:
            flags.append("正在检查")
        elif state and state.upstream:
            if check and check.error:
                flags.append("远程状态未知")
                detail.append(check.error)
            elif not remote_current:
                flags.append("正在检查远程")
        if state and state.blocked:
            flags.append("需处理")
            detail.append(state.blocked)
        if state and state.changes and len(state.changes) > len(state.members):
            detail.append("有其他文件未提交；更新前须清理 Git 工作区")

        summary = " · ".join(flags) or ("已同步" if path else "未打开导图")
        text = f"Git 同步 · {summary}"
        if self._last_text != text:
            self._last_text = text
            self.button.setText(text)
            self.button.setAccessibleName(text)
        self.button.setToolTip("\n".join((text, *(detail or ([str(path)] if path else [])))))
        branch = state.branch if state and state.branch else "当前导图"
        self.description.setText(f"{branch} · {summary}".replace("&", "&&"))
        self.description.setToolTip("\n".join(detail))

        ready = bool(state and state.head and not state.blocked and not busy and not checking
                     and document and not document.file_state.conflict)
        self.actions[_CHECK].setEnabled(path is not None and not busy and not checking)
        self.actions[_UPDATE].setEnabled(bool(ready and remote_current and state.behind and not state.ahead
                                              and not state.changes and not pending))
        can_commit = bool(ready and not ignored and (state.members or pending))
        self.actions[_COMMIT].setEnabled(can_commit)
        self.actions[_SYNC].setEnabled(bool(ready and not ignored and state.upstream and (can_commit or state.ahead)
                                            and (not state.behind or not remote_current)))
        self.actions[_SYNC].setText("推送" if state and state.ahead and not can_commit else _SYNC)
        palette = self.window.context.ui
        color = palette.theme_color("warning" if flags else "text-default").name()
        appearance = (color, palette.theme_color("text-default").name())
        if self._appearance != appearance:
            self._appearance = appearance
            self.button.setIcon(palette.theme_icon("git-sync"))
            self.button.setStyleSheet(f"""
                QToolButton#mindmap_git_sync {{ color:{color}; }}
                QToolButton#mindmap_git_sync::menu-indicator {{
                    subcontrol-position: right center;
                    right: 7px;
                }}
            """)

    def _start(self, label):
        path, document, state = self.path, self.document, self.status
        if (path is None or document is None or state is None or not self.actions[label].isEnabled()
                or self._busy(path) or self._has(path, "remote")):
            return
        repository = GitRepository(path)
        if label != _UPDATE:
            # 点击时立即检查候选并提交有效输入，避免联网等待期间切换标签丢失输入提示。
            if not document.prepare_inputs():
                return
        self._run_action(document, repository, label, lambda: repository.prepare_action(state, remote=label != _COMMIT),
                         lambda prepared: self._write(document, repository, label, *prepared))

    def _run_action(self, document, repository, label, run, done):
        path = repository.path
        self._submit(path, "action", run, done, lambda error: self._action_failed(document, path, label, error))
        self._render()

    def _write(self, document, repository, label, state, check):
        path = repository.path
        if self._current(path) and self.document is document:
            self.status = state
            if check is not None:
                self.remote = check
                self._notify_remote()
        if document.disposed or document.path != path:
            self._action_done(document, path, "导图已关闭或另存，本次操作已取消", level="warning")
            return
        if label == _UPDATE:
            if check is None or check.error:
                self._action_done(document, path, check.error if check else "当前分支未设置远程上游", level="warning")
                return
            if (not document.prepare_inputs() or document.is_dirty or document.is_writing or state.changes
                    or state.ahead or document.last_error):
                self._action_done(document, path, "本地有修改，请先保存或处理本地修改", level="warning")
                return
        else:
            # 用户已点提交；将各标签的有效输入写盘。大图使用既有后台保存进程，
            # 由 40ms 轮询等待最新快照，避免在 GUI 线程阻塞。
            if not document.prepare_inputs() or document.file_state.conflict:
                self._action_done(document, path, "请先完成输入或处理保存冲突", level="warning")
                return
            if document.is_writing or document.is_dirty:
                self._wait_for_save(document, repository, label, state, check)
                return
        self._write_saved(document, repository, label, state, check)

    def _wait_for_save(self, document, repository, label, state, check, *, resumed=False):
        path = repository.path
        if (document.disposed or document.path != path or document.file_state.conflict
                or (resumed and document.last_error)):
            self._action_done(document, path, "导图未保存，已停止 Git 操作", level="warning")
            return
        if not document.is_writing and not document.autosave(background=True):
            self._action_done(document, path, "导图未保存，已停止 Git 操作", level="warning")
            return
        if document.is_writing:
            # 只等待已经提交给后台进程的保存；用户切换或关闭时仍按普通文档规则处理。
            self._waiting_save[path] = document, repository, label, state, check
            self._poll.start()
            self._render()
        else:
            self._write_saved(document, repository, label, state, check)

    def _write_saved(self, document, repository, label, state, check):
        path = repository.path
        if (not document.prepare_inputs() or document.file_state.version is None
                or not document.begin_git_write()):
            self._action_done(document, path, "导图还有未完成的编辑或保存，请稍后重试", level="warning")
            return
        version = document.file_state.version
        if label == _UPDATE:
            self._run_action(document, repository, label, lambda: repository.fast_forward(check, state.head, version),
                             lambda changed: self._reloaded(document, path, changed))
        else:
            self._run_action(document, repository, label, lambda: repository.commit(state, version),
                             lambda head: self._committed(document, repository, label, state, check, head))

    def _reloaded(self, document, path, changed):
        if changed:
            try:
                if not document.reload_preserving_views():
                    raise GitSyncError("已取消重新加载")
            except Exception as exc:
                error = DirectoryVersionConflict(f"Git 已更新，但导图未重新加载：{exc}")
                self._action_failed(document, path, _UPDATE, error)
                return
        self._action_done(document, path, "已更新导图" if changed else "导图已是最新版本")

    def _committed(self, document, repository, label, state, check, head):
        path = repository.path
        committed = head != state.head
        if label == _COMMIT:
            self._action_done(document, path, "已提交导图" if committed else "没有需要提交的导图修改")
        elif check is None or check.error or state.behind:
            detail = check.error if check and check.error else "远程有更新，请先处理两边的修改"
            prefix = "已提交到本地" if committed else "未推送"
            self._action_done(document, path, f"{prefix}：{detail}", level="warning")
        else:
            document.finish_git_write()
            self._run_action(document, repository, label, lambda: repository.push(check, head),
                             lambda pushed: self._pushed(document, path, check, head, pushed))

    def _pushed(self, document, path, check, head, pushed):
        if pushed and self._current(path):
            self.remote = RemoteCheck(check.upstream, head, url=check.url)
        self._action_done(document, path, "已推送导图" if pushed else "没有需要推送的提交")

    def _action_failed(self, document, path, label, error):
        if isinstance(error, DirectoryVersionConflict) and not document.disposed:
            document.files.report_error(error)
        self._action_done(document, path, f"{label}未完成：{error}", level="error")

    def _action_done(self, document, path, message, *, level="info"):
        document.finish_git_write()
        self.window.record_operation(message, session=path, level=level)
        if self._current(path):
            self.window.show_status_message(message)
            self._read_local()
            self._render()

    def dispose(self) -> None:
        """停止轮询和界面回调，忽略已关闭窗口上的后台查询结果。"""
        if self._disposed:
            return
        self._disposed = True
        for timer in (self._local_timer, self._remote_timer, self._poll):
            timer.stop()
        self._jobs.clear()
        self._waiting_save.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)
