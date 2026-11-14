"""以独立导图目录为根的 Git 操作。只处理文件和 Git 状态，不访问 Qt 或编辑引擎。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mindmap_core import MindMapError
from mindmap_core.formats import (
    DirectoryVersion,
    check_directory_version,
    guard_directory_version,
    is_directory_member,
)
from mindmap_core.formats.directory_storage import MANIFEST

_OPERATIONS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge",
               "rebase-apply", "sequencer", "BISECT_START")
_HEAD_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class GitSyncError(MindMapError):
    """可以直接显示在界面上的 Git 错误，不携带可能含有凭据的远程地址或原始输出。"""


@dataclass(frozen=True)
class Upstream:
    """绑定当前本地分支、取回位置及推送目的地，供查询和写入间重新核对。"""

    branch: str
    tracking_ref: str
    remote: str
    remote_ref: str


@dataclass(frozen=True)
class Change:
    """从 Git 状态得到一条相对于工作区根的变更，重命名以删除和新增分别报告。"""

    path: str
    code: str


@dataclass(frozen=True)
class GitStatus:
    """一次本地查询快照。ahead/behind 比较已取回的引用，不能用于证明远程在线。"""

    branch: str = ""
    head: str = ""
    upstream: Upstream | None = None
    remote_head: str = ""
    ahead: int = 0
    behind: int = 0
    changes: tuple[Change, ...] = ()
    problem: str = ""
    blocked: str = ""

    @property
    def members(self) -> tuple[Change, ...]:
        """仅返回导图成员的变更；同时保留 changes 供快进前检查整个工作区。"""
        return tuple(change for change in self.changes if is_directory_member(change.path))


@dataclass(frozen=True)
class RemoteCheck:
    """一次远程查询结果。地址只用于核对配置，不进入界面或日志。"""

    upstream: Upstream
    head: str = ""
    error: str = ""
    url: str = ""


def _parse_status(output: str) -> tuple[dict[str, str], tuple[Change, ...]]:
    """解析 Git 的 v2 NUL 输出，不用引号或换行猜测文件名。"""
    branch = {}
    changes = []
    for record in output.split("\0"):
        if record.startswith("# "):
            key, _, value = record[2:].partition(" ")
            branch[key] = value
        elif record.startswith("1 "):
            fields = record.split(" ", 8)
            changes.append(Change(fields[8], fields[1]))
        elif record.startswith("u "):
            changes.append(Change(record.split(" ", 10)[10], "UU"))
        elif record.startswith("? "):
            changes.append(Change(record[2:], "??"))
    return branch, tuple(changes)


def _git_hint(stderr: str) -> str:
    """将常见 Git 诊断译成可操作的提示。刻意略去可能回显 URL 或凭据的原始内容。"""
    value = stderr.casefold()
    if any(word in value for word in ("authentication", "credential", "permission denied", "repository not found",
                                      "could not read username", "terminal prompts disabled")):
        return "请检查远程访问权限和系统 Git 凭据"
    if any(word in value for word in ("couldn't find remote ref", "no such ref")):
        return "远程分支已不存在，请检查上游设置"
    if any(word in value for word in ("could not resolve", "connection", "unable to access", "network")):
        return "请检查网络和远程仓库是否可用"
    if "identity unknown" in value or "auto-detect email" in value:
        return "请先在 Git 中配置用户名和邮箱"
    if any(word in value for word in ("non-fast-forward", "fetch first", "stale info", "remote rejected")):
        return "远程拒绝了推送，请重新检查远程状态和分支权限"
    if "overwrite" in value or "cannot lock" in value or "lock file" in value:
        return "工作区有变化或正在被其他 Git 操作使用"
    return "请检查仓库状态和 Git 配置后重试"


class GitRepository:
    """一次性后台操作所用的 Git 适配器，所有路径均依附传入的已打开导图。

    不创建仓库、不更改 Git 配置；无控制台、无交互式登录。每个写操作重新确认
    仓库根和分支身份，网络操作不持有导图目录锁。调用方在 Qt 工作线程外部负责
    调度，并在改变工作区期间暂缓同一导图的编辑和保存。
    """

    def __init__(self, path: Path):
        self.path = path
        self.git = shutil.which("git")

    def _run(self, title: str, *args: str, timeout: int = 12, allow_failure: bool = False,
             stdin: str | None = None) -> str:
        """用参数列表启动 Git。输出统一解码，失败时抛出可展示的操作提示。"""
        if self.git is None:
            raise GitSyncError("未找到 Git，请安装 Git 后重试")
        env = os.environ.copy()
        # 从终端启动时，不能继承指向另一仓库或暂存区的环境变量。
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_NAMESPACE",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_QUARANTINE_PATH",
                    "GIT_PREFIX", "GIT_IMPLICIT_WORK_TREE", "GIT_ASKPASS"):
            env.pop(key, None)
        env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_NO_LAZY_FETCH="1",
                   SSH_ASKPASS_REQUIRE="never", LC_ALL="C")
        try:
            result = subprocess.run(
                [self.git, "--no-optional-locks", "--literal-pathspecs", "-c", "credential.interactive=never",
                 "-c", "core.askPass=", "-c", "core.fsmonitor=false", "-C", str(self.path), *args],
                stdin=subprocess.DEVNULL if stdin is None else None,
                input=stdin.encode("utf-8") if stdin is not None else None,
                capture_output=True, env=env, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            raise GitSyncError(f"{title}超时，请稍后重试") from None
        except OSError:
            raise GitSyncError(f"无法启动 Git：{title}") from None
        if result.returncode:
            if allow_failure:
                return ""
            raise GitSyncError(f"{title}失败：{_git_hint(result.stderr.decode('utf-8', 'replace'))}")
        return result.stdout.decode("utf-8", "surrogateescape")

    def status(self) -> GitStatus:
        """读取当前根目录和分支状态，不联网，不扫描父仓库，也不更新 Git 索引。"""
        if not (self.path / ".git").exists():
            return GitStatus(problem="导图目录未关联 Git 仓库")
        if self.git is None:
            return GitStatus(problem="未找到 Git，请安装 Git 后重试")
        root = self._run("检查 Git 仓库", "rev-parse", "--show-toplevel").strip("\r\n")
        if Path(root).resolve() != self.path.resolve():
            return GitStatus(problem="导图目录不是 Git 仓库根目录")
        raw = self._run("读取本地状态", "status", "--porcelain=v2", "--branch", "-z",
                        "--untracked-files=all", "--no-renames", "--ignore-submodules=none")
        headers, changes = _parse_status(raw)
        hidden = self._run("检查被忽略的导图文件", "ls-files", "-z", "--others", "--ignored",
                           "--exclude-standard", "--", MANIFEST, "pages", "assets")
        changes += tuple(Change(path, "!!") for path in hidden.split("\0") if is_directory_member(path))
        branch, head = headers["branch.head"], headers["branch.oid"]
        if branch == "(detached)":
            return GitStatus(changes=changes, problem="当前未检出分支，请先切换到分支")
        if head == "(initial)":
            return GitStatus(branch=branch, changes=changes, problem="请先在 Git 中建立初始提交和上游")

        ref = "refs/heads/" + branch
        refs = self._run("读取上游", "for-each-ref",
                         "--format=%(refname)%00%(upstream)%00%(upstream:remotename)%00%(upstream:remoteref)",
                         "--", ref)
        upstream = None
        for fields in (line.split("\0") for line in refs.splitlines()):
            if fields[0] != ref:
                continue
            _, tracking, remote, remote_ref = fields
            if tracking.startswith("refs/remotes/") and remote not in ("", ".") and remote_ref.startswith("refs/heads/"):
                upstream = Upstream(ref, tracking, remote, remote_ref)
            break
        remote_head = ""
        ahead = behind = 0
        if upstream:
            remote_head = self._run("读取上游版本", "rev-parse", "--verify", "--quiet",
                                    upstream.tracking_ref + "^{commit}", allow_failure=True).strip()
            if remote_head:
                counts = self._run("比较分支", "rev-list", "--left-right", "--count", f"{head}...{remote_head}")
                ahead, behind = map(int, counts.split())
        paths = self._run("检查 Git 操作", "rev-parse", "--path-format=absolute",
                          *(word for name in _OPERATIONS for word in ("--git-path", name)))
        blocked = ""
        if any(change.code == "UU" for change in changes) or any(Path(value).exists() for value in paths.splitlines()):
            blocked = "仓库有未完成的 Git 操作，请先在 Git 中处理"
        return GitStatus(branch, head, upstream, remote_head, ahead, behind, changes,
                         "" if upstream else "当前分支未设置远程上游", blocked)

    def fetch(self, upstream: Upstream) -> RemoteCheck:
        """只取回指定的上游分支至其跟踪引用，不改工作区、其他分支、标签或 FETCH_HEAD。"""
        try:
            self._require_current(upstream)
            url = self._remote_url(upstream)
            self._run("检查远程更新", "fetch", "--no-auto-maintenance", "--no-tags", "--no-prune",
                      "--no-prune-tags", "--no-recurse-submodules", "--no-write-fetch-head", "--refmap=",
                      "--", upstream.remote, "+" + upstream.remote_ref + ":" + upstream.tracking_ref, timeout=18)
            head = self._run("读取远程版本", "rev-parse", "--verify", upstream.tracking_ref + "^{commit}").strip()
            if self._remote_url(upstream) != url:
                raise GitSyncError("远程地址在检查期间变化，请重试")
            return RemoteCheck(upstream, head, url=url)
        except GitSyncError as exc:
            return RemoteCheck(upstream, error=str(exc))

    def _require_current(self, upstream: Upstream | None, *, head: str = "") -> GitStatus:
        """验证任务开始时绑定的分支或提交仍可用，拒绝迟到的请求跨分支执行。"""
        state = self.status()
        if not state.head or state.upstream != upstream or (head and state.head != head):
            raise GitSyncError("当前仓库、分支或上游已变化，请检查后重试")
        return state

    def _remote_url(self, upstream: Upstream) -> str:
        return self._run("读取远程地址", "remote", "get-url", "--", upstream.remote).rstrip("\r\n")

    def prepare_action(self, expected: GitStatus, *, remote: bool) -> tuple[GitStatus, RemoteCheck | None]:
        """确认仍在用户点击时的分支，按需检查远程并返回最新本地状态。

        联网失败也返回本地快照和错误，调用方仍可提交。期间若 HEAD 或上游变化，
        整个请求失效，避免用过期的状态修改切换后的分支。
        """
        current = self._require_current(expected.upstream, head=expected.head)
        if current.branch != expected.branch:
            raise GitSyncError("当前分支已变化，请检查后重试")
        if current.blocked:
            raise GitSyncError(current.blocked)
        check = self.fetch(current.upstream) if remote and current.upstream else None
        if check is not None:
            current = self._require_current(check.upstream, head=expected.head)
        return current, check

    def fast_forward(self, check: RemoteCheck, head: str, version: DirectoryVersion) -> bool:
        """对核对过的远程提交做一次快进；未修改工作区返回 False。

        全工作区（包括其他文件的暂存和未跟踪状态）必须干净。持导图锁期间再次
        检查磁盘版本，Git 也必须拒绝覆盖其忽略规则隐藏的文件。
        """
        with guard_directory_version(self.path, version):
            state = self._require_current(check.upstream, head=head)
            self._require_fetched(state, check)
            if state.blocked:
                raise GitSyncError(state.blocked)
            if state.changes or state.ahead:
                raise GitSyncError("本地有修改或待推送提交，已保留两边内容")
            if not state.behind:
                return False
            try:
                self._run("更新导图", "-c", "submodule.recurse=false", "merge", "--ff-only",
                          "--no-autostash", "--no-overwrite-ignore", "--", check.head, timeout=60)
            except GitSyncError as exc:
                error = exc
            else:
                return True
        # 被中断的检出可能只写了部分文件；若磁盘有变化，转入导图的保存冲突流程。
        check_directory_version(self.path, version)
        raise error

    def commit(self, expected: GitStatus, version: DirectoryVersion) -> str:
        """保存后只将导图文件写入新提交，保留其他路径的已暂存内容。

        新文件先登记到索引，再对精确的成员列表使用 --only。若同一受管文件
        已部分暂存，交由外部 Git 选择最终内容；不会覆盖该文件的暂存版本。
        返回新提交的完整标识，无须提交时返回原标识。
        """
        with guard_directory_version(self.path, version):
            state = self._require_current(expected.upstream, head=expected.head)
            if state.branch != expected.branch:
                raise GitSyncError("当前分支已变化，请检查后重试")
            if state.blocked:
                raise GitSyncError(state.blocked)
            if any(change.code == "!!" for change in state.members):
                raise GitSyncError("有导图文件被 Git 忽略，请调整忽略规则后重试")
            if not state.members:
                return state.head
            paths = sorted({change.path for change in state.members})
            if (len(paths) != len(state.members)
                    or any(change.code != "??" and "." not in change.code for change in state.members)):
                raise GitSyncError("有导图文件部分暂存，请先用 Git 处理暂存版本")
            new = [change.path for change in state.members if change.code == "??"]
            if new:
                self._run("登记新导图文件", "add", "--intent-to-add", "--pathspec-from-file=-", "--pathspec-file-nul",
                          stdin="\0".join(new) + "\0")
            self._run("提交导图", "commit", "--only", "-m", "更新导图",
                      "--pathspec-from-file=-", "--pathspec-file-nul", stdin="\0".join(paths) + "\0", timeout=60)
            return self._run("读取提交版本", "rev-parse", "--verify", "refs/heads/" + state.branch).strip()

    def _require_fetched(self, state: GitStatus, check: RemoteCheck) -> None:
        """拒绝使用已失败或已被远程引用、地址变化替换的快照。"""
        if (not _HEAD_ID.fullmatch(check.head) or check.head != state.remote_head
                or self._remote_url(check.upstream) != check.url):
            raise GitSyncError(check.error or "远程状态已变化，请检查后重试")

    def push(self, check: RemoteCheck, head: str) -> bool:
        """推送本次已核对的提交，不依赖 push.default，也不会推送其他分支或标签。"""
        current = self._require_current(check.upstream, head=head)
        self._require_fetched(current, check)
        if current.blocked:
            raise GitSyncError(current.blocked)
        if current.behind:
            raise GitSyncError("远程有更新，已保留本地提交，请先处理两边的修改")
        if not current.ahead:
            return False
        push_urls = self._run("检查推送目标", "remote", "get-url", "--push", "--all", "--",
                              check.upstream.remote).splitlines()
        if push_urls != [check.url]:
            raise GitSyncError("推送地址与远程检查地址不同，请用 Git 推送")
        # 固定本次确认过的提交，即使外部工具之后切换 HEAD，也不会推送另一份内容。
        self._run("推送导图", "push", "--no-mirror", "--no-follow-tags", "--recurse-submodules=no",
                  "--", check.upstream.remote, current.head + ":" + check.upstream.remote_ref, timeout=90)
        return True
