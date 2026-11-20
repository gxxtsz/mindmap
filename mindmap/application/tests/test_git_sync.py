"""用真实 Git 工作区和桌面菜单验证两端同步、损坏保护与共享标签。"""

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from mindmap_app.app import build_window
from mindmap_application import documents
from mindmap_application.git_repository import GitRepository
from mindmap_application.views import MindMapEditorView
from mindmap_core import MindMapDocument, MindMapEngine
from mindmap_core.formats import load_directory, save_directory
from PySide6.QtTest import QSignalSpy


def git(path, *args):
    """仓库由各测试创建，只读取或修改隔离的工作副本和本地远程。"""
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, encoding="utf-8", timeout=15, check=False,
    )
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


def identify(path):
    git(path, "config", "user.name", "导图测试")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "core.autocrlf", "false")


@pytest.fixture
def copies(tmp_path, monkeypatch):
    """一份裸仓库、两个可提交的工作副本，不依赖开发机的 Git 设置或网络。"""
    if shutil.which("git") is None:
        pytest.skip("需安装 Git")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty.gitconfig"))
    server, local, second = (tmp_path / name for name in ("远程.git", "编辑 副本", "另一副本"))
    git(tmp_path, "init", "--bare", "-b", "main", str(server))
    engine = MindMapEngine()
    one = engine.add_child(engine.root_id, "第一张子图")
    two = engine.add_child(engine.root_id, "第二张子图")
    save_directory(engine.document, local)
    git(local, "init", "-b", "main")
    identify(local)
    git(local, "add", "--", "mindmap.json", "pages")
    git(local, "commit", "-m", "导入")
    git(local, "remote", "add", "origin", str(server))
    git(local, "push", "-u", "origin", "main")
    git(tmp_path, "clone", str(server), str(second))
    identify(second)
    return server, local, second, one, two


@pytest.fixture
def window(qtbot, tmp_path):
    result = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(result, before_close_func=lambda item: item.dispose())
    result.resize(1280, 800)
    result.show()
    yield result
    result.dispose()


def open_map(window, qtbot, path):
    assert window.open_path(path)
    sync = window.application.git_sync
    qtbot.waitUntil(lambda: sync.button.text() == "Git 同步 · 已同步", timeout=12000)
    return window.application.active_session, sync


def edit_disk(path, operation):
    engine = MindMapEngine()
    engine.replace_document(load_directory(path))
    operation(engine)
    save_directory(engine.document, path)
    git(path, "add", "-A")
    git(path, "commit", "-m", "另一副本修改")
    git(path, "push")


def check_remote(qtbot, sync):
    qtbot.waitUntil(sync.actions["立即检查"].isEnabled, timeout=12000)
    sync.actions["立即检查"].trigger()


def test_remote_check_and_fast_forward_restore_each_view(window, qtbot, copies):
    server, local, second, one, two = copies
    # 先给两个副本建立相同的子图，之后更新只删除其中一个页面。
    def add_pages(engine):
        engine.create_submap(one)
        engine.create_submap(two)

    edit_disk(second, add_pages)
    git(local, "pull", "--ff-only")
    first, sync = open_map(window, qtbot, local)
    first.canvas.enter_submap(one)
    page_id = first.canvas.current_page_id
    first.canvas.restore_page_view_state({**first.canvas.capture_page_view_state(), "zoom": 1.5})
    assert first.duplicate()
    other = window.application.active_session
    assert other.document is first.document
    other.canvas.restore_page_view_state({"page_id": None})
    other.canvas.enter_submap(two)
    other.canvas.set_editable(False)
    assert sync.button.parentWidget() is window.title_bar
    assert sync.button.geometry().right() < window.recent_files_button.geometry().left()

    messages = QSignalSpy(window.status.messageChanged)
    initial = git(local, "rev-parse", "HEAD")

    def apply_remote(engine):
        engine.update_node(engine.root_id, text="远程版本")
        engine.remove_submap(two)

    edit_disk(second, apply_remote)
    sync._remote_timer.setInterval(100)  # 保留真实周期路径，加快测试中的轮询。
    qtbot.waitUntil(lambda: "远程有更新" in sync.button.text(), timeout=12000)
    sync._remote_timer.setInterval(30000)
    assert git(local, "rev-parse", "HEAD") == initial
    assert other.engine.document.root.text != "远程版本"
    check_remote(qtbot, sync)
    qtbot.waitUntil(lambda: sync.actions["更新导图"].isEnabled(), timeout=12000)
    assert sum("远程有 1 个新提交" in messages.at(i)[0] for i in range(messages.count())) == 1
    sync.actions["更新导图"].trigger()
    qtbot.waitUntil(lambda: sync.button.text() == "Git 同步 · 已同步", timeout=12000)
    assert first.engine.document.root.text == other.engine.document.root.text == "远程版本"
    assert first.canvas.current_page_id == page_id
    assert first.canvas.capture_page_view_state()["zoom"] == pytest.approx(1.5)
    assert other.canvas.current_page_id is None
    assert first.canvas.editable and not other.canvas.editable
    assert git(local, "rev-parse", "HEAD") == git(server, "rev-parse", "main")


def test_commit_push_only_map_files_and_retry_failed_push(window, qtbot, copies):
    server, local, _, one, _ = copies
    session, sync = open_map(window, qtbot, local)
    # 让 fetch 正常、push 失败，确认应保留真实的本地提交。
    git(server, "config", "receive.denyCurrentBranch", "refuse")
    git(server, "config", "receive.hideRefs", "refs/heads/main")
    notes = local / "说明 空格.txt"
    notes.write_text("保留暂存", encoding="utf-8")
    git(local, "add", "--", notes.name)
    staged = git(local, "ls-files", "-s", "--", notes.name)
    notes.write_text("保留暂存和未暂存版本", encoding="utf-8")
    (local / "随手记.txt").write_text("不提交", encoding="utf-8")
    page = session.engine.create_detail_page(one)
    session.engine.set_node_attachment(one, ("随导图保存的附件.txt", "附件内容".encode("utf-8")))
    asset_path = f"assets/{session.engine.get_node(one).attachment.asset_id}"
    assert "待保存" in sync.button.text()
    assert sync.actions["提交并推送"].isEnabled()
    before = git(server, "rev-parse", "main")
    sync.actions["提交并推送"].trigger()
    qtbot.waitUntil(lambda: "待推送" in sync.button.text() and not sync._busy(local), timeout=12000)
    assert not session.engine.is_dirty
    assert git(server, "rev-parse", "main") == before
    assert git(local, "log", "-1", "--format=%s") == "更新导图"
    assert set(git(local, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()) == {
        "mindmap.json", f"pages/{page}.json", "pages/main.json", asset_path,
    }
    assert git(local, "ls-files", "-s", "--", notes.name) == staged
    assert git(local, "status", "--porcelain", "--", "mindmap.json", "pages") == ""
    head = git(local, "rev-parse", "HEAD")
    git(server, "config", "--unset", "receive.hideRefs")
    assert sync.actions["提交并推送"].text() == "推送"
    sync.actions["提交并推送"].trigger()
    qtbot.waitUntil(lambda: sync.button.text() == "Git 同步 · 已同步", timeout=12000)
    assert git(server, "rev-parse", "main") == head == git(local, "rev-parse", "HEAD")
    assert git(server, "cat-file", "-p", f"main:{asset_path}") == "附件内容"
    assert git(local, "ls-files", "-s", "--", notes.name) == staged


def test_divergence_allows_only_commit_without_losing_changes(window, qtbot, copies):
    server, local, second, _, _ = copies
    session, sync = open_map(window, qtbot, local)
    edit_disk(second, lambda engine: engine.update_node(engine.root_id, text="远程修改"))
    session.engine.update_node(session.engine.root_id, text="本地修改")
    assert session.document.autosave()
    check_remote(qtbot, sync)
    qtbot.waitUntil(lambda: all(word in sync.button.text() for word in ("远程有更新", "未提交")), timeout=12000)
    assert not sync.actions["更新导图"].isEnabled()
    assert not sync.actions["提交并推送"].isEnabled()
    assert sync.actions["仅提交"].isEnabled()
    remote_before = git(server, "rev-parse", "main")
    sync.actions["仅提交"].trigger()
    qtbot.waitUntil(lambda: "待推送" in sync.button.text() and not sync._busy(local), timeout=12000)
    assert not sync.actions["提交并推送"].isEnabled()
    assert not sync.actions["更新导图"].isEnabled()
    assert git(server, "rev-parse", "main") == remote_before
    assert load_directory(local).root.text == "本地修改"
    assert session.engine.document.root.text == "本地修改"
    assert git(local, "rev-list", "--left-right", "--count", "HEAD...origin/main").split() == ["1", "1"]


def test_offline_does_not_hide_local_changes_or_target_parent_repo(window, qtbot, copies, tmp_path):
    server, local, _, _, _ = copies
    session, sync = open_map(window, qtbot, local)
    git(local, "remote", "set-url", "origin", str(tmp_path / "找不到的远端"))
    check_remote(qtbot, sync)
    qtbot.waitUntil(lambda: "远程状态未知" in sync.button.text(), timeout=12000)
    session.engine.update_node(session.engine.root_id, text="离线可保存")
    qtbot.waitUntil(lambda: "未提交" in sync.button.text(), timeout=12000)
    assert "远程状态未知" in sync.button.text()
    assert sync.actions["仅提交"].isEnabled()
    other = local / "不是仓库根"
    save_directory(MindMapDocument.new(), other)
    assert window.open_path(other)
    qtbot.waitUntil(lambda: "未关联 Git 仓库" in sync.button.text(), timeout=12000)
    assert not sync.actions["提交并推送"].isEnabled()
    assert not sync.actions["仅提交"].isEnabled()

    git(local, "remote", "set-url", "origin", str(server))
    window.context.documents.activate_view(session.id)
    qtbot.waitUntil(lambda: "未提交" in sync.button.text() and "未知" not in sync.button.text(), timeout=12000)
    qtbot.waitUntil(lambda: sync.actions["提交并推送"].isEnabled(), timeout=12000)
    sync.actions["提交并推送"].trigger()
    qtbot.waitUntil(lambda: sync.button.text() == "Git 同步 · 已同步", timeout=12000)
    assert git(server, "rev-parse", "main") == git(local, "rev-parse", "HEAD")


def test_invalid_remote_data_preserves_memory_and_pauses_autosave(window, qtbot, copies):
    _, local, second, _, _ = copies
    session, sync = open_map(window, qtbot, local)
    text = session.engine.document.root.text
    manifest = second / "mindmap.json"
    manifest.write_text("{不合法的清单", encoding="utf-8")
    git(second, "add", "--", manifest.name)
    git(second, "commit", "-m", "远端损坏")
    git(second, "push")
    check_remote(qtbot, sync)
    qtbot.waitUntil(sync.actions["更新导图"].isEnabled, timeout=12000)
    sync.actions["更新导图"].trigger()
    qtbot.waitUntil(lambda: "保存冲突" in sync.button.text(), timeout=12000)
    assert session.engine.document.root.text == text
    assert not session.document.git_busy and session.canvas.editable
    session.engine.update_node(session.engine.root_id, text="保留内存草稿")
    assert not session.document.timer.isActive()
    assert not session.document.autosave()
    assert (local / manifest.name).read_bytes() == manifest.read_bytes()
    assert not sync.actions["提交并推送"].isEnabled()


def test_update_rechecks_worktree_before_checkout(window, qtbot, copies, monkeypatch, tmp_path):
    _, local, second, _, _ = copies
    session, sync = open_map(window, qtbot, local)
    edit_disk(second, lambda engine: engine.update_node(engine.root_id, text="远端已更新"))
    check_remote(qtbot, sync)
    qtbot.waitUntil(sync.actions["更新导图"].isEnabled, timeout=12000)
    before = git(local, "rev-parse", "HEAD")
    began, release = Event(), Event()
    original = GitRepository.fast_forward

    def wait_for_external_change(repo, *args):
        began.set()
        assert release.wait(8)
        return original(repo, *args)

    monkeypatch.setattr(GitRepository, "fast_forward", wait_for_external_change)
    sync.actions["更新导图"].trigger()
    try:
        qtbot.waitUntil(began.is_set, timeout=12000)
        assert session.document.git_busy
        assert not session.canvas.editable
        assert not session.document.autosave()
        assert not window.save()
        assert not session.duplicate()
        assert not window.close()
        assert window.new_document(tmp_path / "另一张导图")
        qtbot.waitUntil(lambda: "未关联 Git 仓库" in sync.button.text(), timeout=12000)
        notes = local / "临时记录.txt"
        notes.write_text("正在编辑时由外部创建", encoding="utf-8")
    finally:
        release.set()
    qtbot.waitUntil(lambda: not session.document.git_busy, timeout=12000)
    assert git(local, "rev-parse", "HEAD") == before
    assert notes.read_text(encoding="utf-8") == "正在编辑时由外部创建"
    assert session.canvas.editable and "未关联 Git 仓库" in sync.button.text()


def test_ignored_map_file_is_reported_without_committing_incomplete_directory(window, qtbot, copies):
    _, local, _, one, _ = copies
    session, sync = open_map(window, qtbot, local)
    page = session.engine.create_detail_page(one)
    (local / ".gitignore").write_text(f"/pages/{page}.json\n", encoding="utf-8")
    assert session.document.autosave()
    qtbot.waitUntil(lambda: "被 Git 忽略" in sync.button.toolTip(), timeout=12000)
    assert "未提交" in sync.button.text()
    assert not sync.actions["仅提交"].isEnabled()
    assert not sync.actions["提交并推送"].isEnabled()


def test_commit_waits_for_ime_and_background_save(window, qtbot, copies, monkeypatch):
    server, local, _, _, _ = copies
    session, sync = open_map(window, qtbot, local)
    composing = True
    monkeypatch.setattr(MindMapEditorView, "is_composing", property(lambda self: composing))
    session.engine.update_node(session.engine.root_id, text="后台快照")
    session.document.timer.stop()
    before = git(local, "rev-parse", "HEAD")
    sync.actions["提交并推送"].trigger()
    qtbot.waitUntil(lambda: "请先完成输入" in window.status.currentMessage(), timeout=12000)
    assert git(local, "rev-parse", "HEAD") == before and session.engine.is_dirty

    composing = False
    monkeypatch.setattr(documents, "BACKGROUND_SAVE_NODE_LIMIT", 1)
    session.engine.add_child(session.engine.root_id, "写入时保留")
    session.document.timer.stop()
    began, release = Event(), Event()
    commit = GitRepository.commit

    def assert_saved(repo, status, version):
        assert release.is_set()
        assert load_directory(repo.path).root.text == "后台快照"
        return commit(repo, status, version)

    def save_snapshot(*args, **kwargs):
        began.set()
        assert release.wait(8)
        return original_save(*args, **kwargs)

    original_save = documents.save_snapshot
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(documents, "_SAVE_POOL", executor)
        monkeypatch.setattr(documents, "save_snapshot", save_snapshot)
        monkeypatch.setattr(GitRepository, "commit", assert_saved)
        try:
            qtbot.waitUntil(sync.actions["提交并推送"].isEnabled, timeout=12000)
            sync.actions["提交并推送"].trigger()
            qtbot.waitUntil(began.is_set, timeout=12000)
            assert session.document.is_writing
            assert "正在同步" in sync.button.text() and "待保存" in sync.button.text()
            assert git(local, "rev-parse", "HEAD") == before
        finally:
            release.set()
        qtbot.waitUntil(lambda: sync.button.text() == "Git 同步 · 已同步", timeout=12000)
    assert git(server, "rev-parse", "main") != before
    assert load_directory(local).root.text == "后台快照"
