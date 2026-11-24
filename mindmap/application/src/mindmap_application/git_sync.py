"""把后台 Git 检查、共享文档和顶栏同步菜单连接起来。"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from mindmap_core import ValidatedDocument
from mindmap_core.formats import DirectoryVersion, DirectoryVersionConflict, load_validated_directory
from mindmap_desktop_api import path_key
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
_AUTO_INTERVAL = 3_600_000
_AUTO_PATHS = "git/auto_sync_paths"
_AUTO = "每小时自动同步"
_CHECK, _UPDATE, _COMMIT, _SYNC = "立即检查", "更新导图", "仅提交", "提交并推送"


@dataclass
class _Task:
    """工作线程只持有 future 的输入；文档和回调始终由 GUI 线程保存和访问。"""

    future: Future
    done: Callable
    failed: Callable[[Exception], None]


@dataclass
class _AutoSchedule:
    """一份已启用导图的小时计时。对象身份也用于识别关闭、改绑前提交的旧任务。"""

    path: Path
    due_at: float
    queued: bool = False


_SavedAction = tuple["MindMapDocumentHandle", GitRepository, str, GitStatus, RemoteCheck | None]
_LoadedDirectory = tuple[ValidatedDocument, DirectoryVersion]


def _fast_forward_and_load(
    repository: GitRepository, check: RemoteCheck, head: str, version: DirectoryVersion,
) -> _LoadedDirectory | None:
    """在工作线程快进并读取已更新的目录。加载失败时将已变化的磁盘标记为保存冲突。

    Git 写入结束后才重新取得目录锁读取，避免嵌套持锁。没有更新时返回空结果。
    """
    if not repository.fast_forward(check, head, version):
        return None
    try:
        return load_validated_directory(repository.path)
    except Exception as error:
        raise DirectoryVersionConflict(f"Git 已更新，但导图未重新加载：{error}") from error


class GitSyncController(QObject):
    """显示活动导图的 Git 状态，并为启用的共享文档调度定时同步。

    同一路径的写操作串行，本地和远程检查有独立计时器。慢速远程检查期间仍可
    读取本地状态。定时任务逐份处理，并通过文档、路径及计时身份识别迟到的结果。
    """

    def __init__(self, window: MindMapController):
        super().__init__(window)
        self.window = window
        self.document: MindMapDocumentHandle | None = None
        self.path: Path | None = None
        self._path_key: str | None = None
        self.status: GitStatus | None = None
        self.remote: RemoteCheck | None = None
        self.local_error = ""
        self._disposed = False
        self._jobs: dict[tuple[Path, str], _Task] = {}
        self._notified: dict[tuple[Path, Upstream], str] = {}
        self._waiting_save: dict[Path, _SavedAction] = {}
        paths = window.preferences.value(_AUTO_PATHS, [])
        self._auto_paths = {path for path in paths if isinstance(path, str)} if isinstance(paths, list) else set()
        self._auto_schedules: dict[MindMapDocumentHandle, _AutoSchedule] = {}
        self._auto_running: _AutoSchedule | None = None
        self._local_again = False
        self._want_remote = False
        self._manual_check = False
        self._appearance = None
        self._last_text = ""
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mindmap-git")
        self._local_timer = self._timer(_LOCAL_INTERVAL, self._read_local)
        self._remote_timer = self._timer(_REMOTE_INTERVAL, self._request_remote)
        self._auto_timer = self._timer(_AUTO_INTERVAL, self._queue_auto_sync)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setTimerType(Qt.TimerType.PreciseTimer)
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
        self.menu.addSeparator()
        auto = self.actions[_AUTO] = self.menu.addAction(_AUTO)
        auto.setCheckable(True)
        auto.setToolTip("打开这份导图时，每小时同步当前分支与其上游")
        auto.triggered.connect(self._set_auto_sync)
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
            self._path_key = path_key(path) if path is not None else None
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

    def track_document(self, document: MindMapDocumentHandle) -> None:
        """在打开、另存或关闭共享文档时更新定时同步登记，仅由 GUI 线程调用。

        启用的目录首次登记时从当前时刻计时。同一文档的其他标签沿用原计时。
        路径改绑或最后一个标签关闭后，旧登记上的等待和检查结果随即失效。
        """
        if self._disposed:
            return
        path = None if document.disposed else document.path
        previous = self._auto_schedules.get(document)
        if path is not None and path_key(path) in self._auto_paths:
            if previous is not None and previous.path == path:
                return
            self._auto_schedules[document] = _AutoSchedule(path, monotonic() + _AUTO_INTERVAL / 1000)
        elif previous is not None:
            del self._auto_schedules[document]
        else:
            return
        self._arm_auto_timer()

    def _set_auto_sync(self, checked: bool) -> None:
        """持久化当前目录的开关，写入成功后才调整计时。"""
        document, key = self.document, self._path_key
        if self._disposed or document is None or key is None:
            return
        paths = self._auto_paths | {key} if checked else self._auto_paths - {key}
        settings = self.window.preferences
        previous = settings.value(_AUTO_PATHS, [])
        settings.setValue(_AUTO_PATHS, sorted(paths))
        try:
            settings.sync()
        except (OSError, ValueError, TypeError) as error:
            settings.setValue(_AUTO_PATHS, previous)
            message = f"保存自动同步设置失败：{error}"
            self.window.record_operation(message, session=self.path, level="error")
            self.window.show_status_message(message)
        else:
            self._auto_paths = paths
            self.track_document(document)
        self._render()

    def _arm_auto_timer(self) -> None:
        """单个计时器等待最早到期的目录，标签切换不会改变各自的目标时刻。"""
        if not self._auto_schedules:
            self._auto_timer.stop()
            return
        earliest = min(schedule.due_at for schedule in self._auto_schedules.values())
        self._auto_timer.start(max(1, ceil((earliest - monotonic()) * 1000)))

    def _queue_auto_sync(self) -> None:
        """每个到期目录只保留一个请求。正在写盘时跳过本轮，避免读取保存事务的中间状态。"""
        if self._disposed:
            return
        now = monotonic()
        for document, schedule in self._auto_schedules.items():
            if schedule.due_at <= now:
                schedule.due_at = now + _AUTO_INTERVAL / 1000
                schedule.queued = schedule is not self._auto_running and not document.is_writing
        self._arm_auto_timer()
        self._start_due_auto_sync()

    def _start_due_auto_sync(self) -> None:
        """同一路径的现有任务结束后接续定时检查，逐份处理到期的导图。"""
        if self._disposed or self._auto_running is not None:
            return
        for document, schedule in self._auto_schedules.items():
            path = schedule.path
            if (not schedule.queued or self._busy(path) or self._has(path, "local", "remote")
                    or document.git_busy):
                continue
            schedule.queued = False
            if not self._auto_current(document, schedule) or document.is_writing:
                continue
            self._start_auto_sync(document, schedule)
            break

    def _auto_current(self, document: MindMapDocumentHandle, schedule: _AutoSchedule) -> bool:
        """每个写入步骤派发前，确认它仍归属于原文档、原目录和原开关登记。"""
        return (not self._disposed and self._auto_schedules.get(document) is schedule
                and not document.disposed and document.path == schedule.path)

    def _current(self, path: Path) -> bool:
        return not self._disposed and self.path == path and self.document is not None

    def _has(self, path: Path | None, *kinds: str) -> bool:
        return any((path, kind) in self._jobs for kind in kinds)

    def _busy(self, path: Path | None) -> bool:
        return (self._has(path, "action") or path in self._waiting_save
                or (self._auto_running is not None and self._auto_running.path == path))

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
        self._start_due_auto_sync()
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

    @staticmethod
    def _pending_save(document: MindMapDocumentHandle | None) -> bool:
        """只读检查共享文档的各个视图和保存状态，保留正在输入的草稿和候选。"""
        return bool(document and (document.is_dirty or document.is_writing or any(
            view.canvas.is_editing or view.is_composing or view.panels.has_pending_edits for view in document.views)))

    def _render(self, *_):
        if self._disposed:
            return
        state, check, path, document = self.status, self.remote, self.path, self.document
        busy = self._busy(path) or bool(document and document.git_busy)
        checking = self._has(path, "remote")
        pending = self._pending_save(document)
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
        auto = self.actions[_AUTO]
        enabled = self._path_key in self._auto_paths
        auto.setChecked(enabled)
        auto.setEnabled(enabled or bool(state and state.head and state.upstream))
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

    def _auto_writable(self, document: MindMapDocumentHandle) -> bool:
        """仅使用已确认的落盘内容。定时任务不会提交界面草稿或代替用户处理保存错误。"""
        return not (self._pending_save(document) or document.file_state.conflict or document.last_error)

    def _start_auto_sync(self, document: MindMapDocumentHandle, schedule: _AutoSchedule) -> None:
        """为到期目录独立取回上游。任务和结果均绑定该轮的文档和计时登记，允许切换标签。"""
        path = schedule.path
        repository = GitRepository(path)
        can_write = self._auto_writable(document)
        self._auto_running = schedule

        def failed(error):
            if self._auto_current(document, schedule):
                self._action_failed(document, path, _AUTO, error)
            else:
                self._action_done(document, path)

        self._submit(path, "action", repository.prepare_auto_sync,
                     lambda result: self._write_auto(document, schedule, repository, can_write, *result), failed)
        self._render()

    def _write_auto(
        self, document: MindMapDocumentHandle, schedule: _AutoSchedule, repository: GitRepository,
        can_write: bool, state: GitStatus, check: RemoteCheck | None,
    ) -> None:
        """用本轮新取回的 Git 状态选择快进、提交或推送，再检查各标签能否安全写入。"""
        path = schedule.path
        if not self._auto_current(document, schedule):
            self._action_done(document, path)
            return
        if self._current(path) and self.document is document:
            self.status, self.remote, self.local_error = state, check, ""
        if check is None or check.error or state.blocked or state.problem:
            problem = state.blocked or state.problem or (check.error if check else "当前分支未设置远程上游")
            self._action_done(document, path, f"自动同步已跳过：{problem}", level="warning")
            return
        if state.behind and (state.ahead or state.changes):
            self._action_done(document, path, "自动同步已跳过，远程有更新，请先处理本地修改或分叉", level="warning")
            return
        version = document.file_state.version
        if not can_write or version is None or not self._auto_writable(document):
            self._action_done(document, path)
            return
        if not (state.behind or state.members):
            if state.ahead:
                self._push_auto(document, schedule, repository, check, state.head, version)
            else:
                self._action_done(document, path)
            return
        if not document.begin_git_write() or not self._auto_current(document, schedule):
            self._action_done(document, path)
            return
        if state.behind:
            self._run_action(document, repository, _AUTO,
                             lambda: _fast_forward_and_load(repository, check, state.head, version),
                             lambda loaded: self._reloaded(document, path, loaded is not None,
                                                           label=_AUTO, prepared=loaded))
        else:
            self._run_action(document, repository, _AUTO, lambda: repository.commit(state, version, check=check),
                             lambda head: self._push_auto(document, schedule, repository, check, head, version,
                                                          committed=head != state.head))

    def _push_auto(
        self, document: MindMapDocumentHandle, schedule: _AutoSchedule, repository: GitRepository,
        check: RemoteCheck, head: str, version: DirectoryVersion, *, committed: bool = False,
    ) -> None:
        """本地提交结束即可恢复编辑，仍以本轮校验的提交标识推送。

        开关、文档或路径已经改变时，结束后续步骤。若提交已成功，记录本地待推送状态。
        """
        path = schedule.path
        if document.path == path:
            document.finish_git_write()
        if not self._auto_current(document, schedule) or not self._auto_writable(document):
            self._action_done(document, path, "已自动提交导图，待推送" if committed else None)
            return
        self._run_action(document, repository, _AUTO, lambda: repository.push(check, head, version=version),
                         lambda pushed: self._pushed(document, path, check, head, pushed, automatic=True))

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

    def _reloaded(self, document, path, changed, *, label=_UPDATE, prepared: _LoadedDirectory | None = None):
        """在 GUI 线程接管更新的导图并恢复视图。已下发的快进即使关闭开关也须完成重载。"""
        if document.disposed or document.path != path:
            self._action_done(document, path)
            return
        if changed:
            try:
                if not document.reload_preserving_views(prepared=prepared):
                    raise GitSyncError("已取消重新加载")
            except Exception as exc:
                error = DirectoryVersionConflict(f"Git 已更新，但导图未重新加载：{exc}")
                self._action_failed(document, path, label, error)
                return
        if label == _AUTO:
            message = "已自动更新导图" if changed else None
        else:
            message = "已更新导图" if changed else "导图已是最新版本"
        self._action_done(document, path, message)

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

    def _pushed(self, document, path, check, head, pushed, *, automatic=False):
        if pushed and self._current(path) and self.document is document:
            self.remote = RemoteCheck(check.upstream, head, url=check.url)
        if automatic:
            message = "已自动推送导图" if pushed else None
        else:
            message = "已推送导图" if pushed else "没有需要推送的提交"
        self._action_done(document, path, message)

    def _action_failed(self, document, path, label, error):
        if isinstance(error, DirectoryVersionConflict) and not document.disposed and document.path == path:
            document.files.report_error(error)
        self._action_done(document, path, f"{label}未完成：{error}", level="error")

    def _action_done(self, document, path, message=None, *, level="info"):
        # 另存后的文档可能已经取得新目录的锁，迟到的结果只结束原目录的任务。
        if document.path == path:
            document.finish_git_write()
        if self._auto_running is not None and self._auto_running.path == path:
            self._auto_running = None
        if message:
            self.window.record_operation(message, session=path, level=level)
        if self._current(path):
            if message and self.document is document:
                self.window.show_status_message(message)
            self._read_local()
            self._render()

    def dispose(self) -> None:
        """停止轮询和界面回调，忽略已关闭窗口上的后台查询结果。"""
        if self._disposed:
            return
        self._disposed = True
        for timer in (self._local_timer, self._remote_timer, self._auto_timer, self._poll):
            timer.stop()
        self._jobs.clear()
        self._waiting_save.clear()
        self._auto_schedules.clear()
        self._auto_running = None
        self._pool.shutdown(wait=False, cancel_futures=True)
