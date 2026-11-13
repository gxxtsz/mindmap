"""导图业务日志：节点、分支、页面、保存与导出结果的中文描述。"""

from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from mindmap_app.app import build_window
from mindmap_application import documents as sessions_module
from mindmap_application.operation_log import describe_change
from mindmap_core import BranchStyle, LayoutConfig, MindMapError, Theme
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox


@pytest.fixture
def log_window(qtbot, tmp_path):
    """隔离配置与导图路径，清理阶段恢复失败注入，确保真实退出及资源释放。"""
    window = build_window(storage_dir=tmp_path / "配置", restore=False)

    def clean(item):
        for session in item.application.sessions.values():
            session.panels.properties.discard_pending()
            session.canvas.finish_edit(commit=False)
            session.files.writer = session.document._write
            session.engine.mark_saved()

    qtbot.addWidget(window, before_close_func=clean)
    window.resize(1200, 820)
    window.show()
    QApplication.processEvents()
    yield window
    if not window._disposed:
        clean(window)
        window.close()


def entries(window):
    """只读取得已呈现的日志，空占位文字不算一条操作记录。"""
    return window.operation_log.plain_text().splitlines()


def create(window, path):
    assert window.new_document(path)
    return window.application.active_session


def structure_tree(window, tmp_path):
    """建立固定父子关系，后续日志不受创建及命名准备步骤影响。"""
    engine = create(window, tmp_path / "节点增删.mindmap").engine
    engine.update_node(engine.root_id, text="第二大脑")
    parent = engine.add_child(engine.root_id, "人文学科")
    child = engine.add_child(parent, "哲学")
    window.operation_log.clear()
    return engine, parent, child


@pytest.mark.parametrize(("operation", "label", "name"), [
    ("child", "新增子节点", "伦理学"),
    ("sibling", "新增同级节点", "逻辑学"),
    ("parent", "插入父节点", "哲学分支"),
])
def test_node_additions_show_name_and_direct_parent_with_undo_redo(log_window, tmp_path, operation, label, name):
    """子节点、同级节点及新插入的父节点均说明真正归属，不借用当前选择判断。"""
    window = log_window
    engine, parent, child = structure_tree(window, tmp_path)
    if operation == "child":
        key = engine.add_child(parent, name)
    elif operation == "sibling":
        key = engine.add_sibling(child, name)
    else:
        key = engine.insert_parent(child, name)
    expected = f"{label}：在「人文学科」下新增节点「{name}」"
    assert len(entries(window)) == 1 and window.operation_log.model.entries[0].message.startswith(expected)
    assert engine.parent_id(key) == parent
    if operation == "parent":
        assert engine.parent_id(child) == key
        assert "将「哲学」从「人文学科」移到「哲学分支」下" in window.operation_log.model.entries[0].message
    assert engine.undo()
    assert window.operation_log.model.entries[-1].message.startswith(
        f"撤销：{label}：从「人文学科」下删除节点「{name}」，共删除1个节点",
    )
    assert engine.get_node(child).text == "哲学" and engine.parent_id(child) == parent
    assert engine.redo()
    assert window.operation_log.model.entries[-1].message.startswith("重做：" + expected)


def test_leaf_deletion_names_parent_and_restores_actual_count(log_window, tmp_path):
    window = log_window
    engine, parent, child = structure_tree(window, tmp_path)
    engine.remove_nodes([child])
    expected = "删除节点：从「人文学科」下删除节点「哲学」，共删除1个节点"
    assert len(entries(window)) == 1 and window.operation_log.model.entries[0].message == expected
    assert engine.undo()
    assert window.operation_log.model.entries[-1].message == "撤销：删除节点：在「人文学科」下恢复节点「哲学」，共恢复1个节点"
    assert engine.parent_id(child) == parent
    assert engine.redo()
    assert window.operation_log.model.entries[-1].message == "重做：" + expected


@pytest.mark.parametrize("folded", [False, True])
def test_branch_deletion_counts_root_and_all_descendants_even_when_folded(log_window, tmp_path, folded):
    window = log_window
    engine, _, branch = structure_tree(window, tmp_path)
    child = engine.add_child(branch, "伦理学")
    engine.add_child(child, "道德")
    engine.add_child(branch, "认识论")
    engine.set_expanded(branch, not folded)
    before = engine.node_count
    window.operation_log.clear()
    engine.remove_nodes([branch])
    expected = "删除节点：从「人文学科」下删除分支「哲学」，共删除4个节点"
    assert engine.node_count == before - 4
    assert len(entries(window)) == 1 and window.operation_log.model.entries[0].message == expected
    assert "伦理学" not in window.operation_log.model.entries[0].message
    assert engine.undo() and engine.node_count == before
    assert window.operation_log.model.entries[-1].message == "撤销：删除节点：在「人文学科」下恢复分支「哲学」，共恢复4个节点"


def test_multi_branch_deletion_deduplicates_overlapping_selection_and_keeps_each_parent(log_window, tmp_path):
    window = log_window
    engine, _, first = structure_tree(window, tmp_path)
    first_child = engine.add_child(first, "伦理学")
    other_parent = engine.add_child(engine.root_id, "艺术学")
    second = engine.add_child(other_parent, "绘画")
    engine.add_child(second, "油画")
    window.operation_log.clear()
    engine.remove_nodes([second, first_child, first, first])
    expected = "删除节点：从「人文学科」下删除分支「哲学」；从「艺术学」下删除分支「绘画」，共删除4个节点"
    assert len(entries(window)) == 1 and window.operation_log.model.entries[0].message == expected
    window.operation_log.copy_all_action.trigger()
    assert expected in QApplication.clipboard().text()


def test_branch_deletion_counts_owned_pages_without_listing_their_roots_twice(log_window, tmp_path):
    """页面根是独立节点，应进入总数，但不能被误当作另一个选中的普通分支。"""
    window = log_window
    engine, _, branch = structure_tree(window, tmp_path)
    engine.add_child(branch, "伦理学")
    owner = engine.add_child(branch, "西方哲学")
    submap = engine.create_submap(owner)
    detail = engine.create_detail_page(owner)
    for page, title in ((submap, "古希腊"), (detail, "概念说明")):
        engine.add_child(engine.document.page_record(page).root_id, title)
    before = engine.node_count
    window.operation_log.clear()
    engine.remove_nodes([branch])
    expected = "删除节点：从「人文学科」下删除分支「哲学」，共删除7个节点"
    assert engine.node_count == before - 7
    assert len(entries(window)) == 1 and window.operation_log.model.entries[0].message == expected
    assert not engine.document.has_page(submap) and not engine.document.has_page(detail)
    assert engine.undo() and engine.node_count == before
    assert window.operation_log.model.entries[-1].message.endswith("共恢复7个节点")
    assert engine.document.has_page(submap) and engine.document.has_page(detail)


@pytest.mark.parametrize(("kind", "label"), [("submap", "子导图"), ("detail", "详情页")])
def test_independent_page_deletion_names_owner_and_counts_page_nodes(log_window, tmp_path, kind, label):
    window = log_window
    engine, _, owner = structure_tree(window, tmp_path)
    page = engine.create_submap(owner) if kind == "submap" else engine.create_detail_page(owner)
    engine.add_child(engine.document.page_record(page).root_id, "页面主题")
    window.operation_log.clear()
    if kind == "submap":
        engine.remove_submap(owner)
    else:
        engine.remove_detail_page(owner)
    expected = f"删除{label}：从入口「哲学」下删除{label}「哲学」，共删除2个节点"
    assert len(entries(window)) == 1 and window.operation_log.model.entries[0].message == expected
    assert engine.get_node(owner).text == "哲学"
    assert engine.undo()
    assert window.operation_log.model.entries[-1].message == f"撤销：删除{label}：在入口「哲学」下恢复{label}「哲学」，共恢复2个节点"


def test_image_node_addition_and_deletion_count_nodes_not_image_assets(log_window, tmp_path):
    window = log_window
    engine, parent, _ = structure_tree(window, tmp_path)
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.red)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    key = engine.add_image_child(parent, bytes(buffer.data()), width=8)
    name = f"无文字节点（{key[:8]}）"
    assert window.operation_log.model.entries[0].message == f"新增图片节点：在「人文学科」下新增节点「{name}」"
    assert len(engine.document.assets) == 1
    engine.remove_nodes([key])
    assert window.operation_log.model.entries[-1].message == f"删除节点：从「人文学科」下删除节点「{name}」，共删除1个节点"
    assert not engine.document.assets


def test_pasted_branch_addition_names_destination_and_counts_new_nodes(log_window, tmp_path):
    window = log_window
    engine, parent, branch = structure_tree(window, tmp_path)
    engine.add_child(branch, "伦理学")
    fragment = engine.copy_fragment([branch])
    window.operation_log.clear()
    engine.paste_fragment(parent, fragment)
    expected = "粘贴节点：在「人文学科」下新增分支「哲学」，共新增2个节点"
    assert len(entries(window)) == 1 and window.operation_log.model.entries[0].message == expected


@pytest.mark.parametrize("deleted", [False, True])
def test_add_delete_parent_names_are_frozen_after_parent_rename_and_removal(log_window, tmp_path, deleted):
    window = log_window
    engine, parent, child = structure_tree(window, tmp_path)
    events = []
    engine.subscribe(events.append)
    if deleted:
        engine.remove_nodes([child])
    else:
        engine.add_child(parent, "伦理学")
    event, message = events[-1], window.operation_log.model.entries[0].message
    engine.update_node(parent, text="更名后的父节点")
    engine.remove_nodes([parent])
    assert "人文学科" in message and describe_change(event) == message
    assert window.operation_log.model.entries[0].message == message


def test_long_multi_deletion_summary_keeps_total_count(log_window, tmp_path):
    window = log_window
    engine, parent, child = structure_tree(window, tmp_path)
    engine.remove_nodes([child])
    engine.update_node(parent, text="长父节点" * 100)
    keys = [engine.add_child(parent, f"主题{index}" * 100) for index in range(4)]
    window.operation_log.clear()
    engine.remove_nodes(keys)
    message = window.operation_log.model.entries[0].message
    assert len(message) <= 2048 and "另有1处删除" in message and message.endswith("共删除4个节点")


def test_changes_are_single_commits_and_preview_reset_save_are_quiet(log_window, tmp_path):
    """事务、空操作、回滚及试算遵循提交边界，日志自身不产生文档历史。"""
    window = log_window
    session = create(window, tmp_path / "编辑.mindmap")
    engine = session.engine
    window.operation_log.clear_action.trigger()
    with engine.transaction("批量增加主题"):
        first = engine.add_child(engine.root_id, "甲")
        engine.add_child(engine.root_id, "乙")
    assert len(entries(window)) == 1 and "批量增加主题" in entries(window)[0]
    assert engine.undo() and engine.redo()
    assert len(entries(window)) == 3
    assert "撤销：批量增加主题" in entries(window)[1]
    assert "重做：批量增加主题" in entries(window)[2]
    engine.update_node(first, text="甲")
    with engine.preview_operation():
        engine.add_child(engine.root_id, "试算不提交")
    with pytest.raises(MindMapError):
        with engine.transaction("不应出现的回滚记录"):
            engine.add_child(engine.root_id, "回滚不提交")
            raise MindMapError("事务失败")
    engine.mark_saved()
    assert len(entries(window)) == 3
    before, revision = engine.document, engine.revision
    window.operation_log.copy_all_action.trigger()
    window.operation_log.clear_action.trigger()
    assert engine.document == before and engine.revision == revision and engine.can_undo


def test_inline_text_log_shows_before_after_and_undo_redo_direction(log_window, tmp_path):
    """正文草稿及取消不记成功；确认后显示前后值，撤销和重做按实际方向描述。"""
    window = log_window
    session = create(window, tmp_path / "文字修改.mindmap")
    engine, canvas = session.engine, session.canvas
    key = engine.add_child(engine.root_id, "哲学")
    window.operation_log.clear()
    canvas.begin_edit(key)
    canvas.editor.setPlainText("取消的草稿")
    assert entries(window) == []
    canvas.finish_edit(commit=False)
    assert entries(window) == [] and engine.get_node(key).text == "哲学"
    canvas.begin_edit(key)
    canvas.editor.setPlainText("哲学导论")
    canvas.finish_edit(commit=True)
    message = "修改节点文字：从「哲学」改为「哲学导论」"
    assert len(entries(window)) == 1 and window.operation_log.model.entries[0].message == message
    assert window.operation_log.model.entries[0].display_parts == ("修改节点文字", "从「哲学」改为「哲学导论」")
    assert engine.undo()
    assert window.operation_log.model.entries[-1].message == "撤销：修改节点文字：从「哲学导论」改为「哲学」"
    assert engine.redo()
    assert window.operation_log.model.entries[-1].message == "重做：" + message
    assert window.operation_log.model.entries[0].message == message


@pytest.mark.parametrize(("before", "after", "old_display", "new_display"), [
    ("", "新增内容", "（空内容）", "新增内容"),
    ("旧内容", "", "旧内容", "（空内容）"),
    ("第一行\n第二行", "第一行 第二行", "第一行↵第二行", "第一行 第二行"),
    ("第一行\n", "第一行", "第一行↵", "第一行"),
    ("甲\t乙", "甲 乙", "甲⇥乙", "甲 乙"),
])
def test_text_change_logs_expose_empty_content_and_control_characters(
    log_window, tmp_path, before, after, old_display, new_display,
):
    """空内容、末尾换行及空白差异不能被压平为看似相同的前后值。"""
    window = log_window
    engine = create(window, tmp_path / "特殊内容.mindmap").engine
    key = engine.add_child(engine.root_id, before)
    window.operation_log.clear()
    engine.update_node(key, text=after)
    assert len(entries(window)) == 1
    assert window.operation_log.model.entries[0].message == f"修改节点文字：从「{old_display}」改为「{new_display}」"
    assert engine.undo()
    assert window.operation_log.model.entries[-1].message == f"撤销：修改节点文字：从「{new_display}」改为「{old_display}」"


@pytest.mark.parametrize("position", ["start", "middle", "end"])
def test_long_text_log_preserves_both_values_and_actual_change_context(log_window, tmp_path, position):
    """长公共内容不能挤掉新文字，也不能让首部、中部或尾部的实际差异被省略掉。"""
    window = log_window
    engine = create(window, tmp_path / "长文字.mindmap").engine
    offset = {"start": 0, "middle": 1800, "end": 3600}[position]
    before = "共" * offset + "旧标记" + "共" * (3600 - offset)
    after = "共" * offset + "新标记" + "共" * (3600 - offset)
    key = engine.add_child(engine.root_id, before)
    window.operation_log.clear()
    engine.update_node(key, text=after)
    entry = window.operation_log.model.entries[0]
    assert len(entry.message) <= 2048 and "旧标记" in entry.message and "新标记" in entry.message
    assert "改为「" in entry.message and entry.message.endswith("」") and "…" in entry.message
    assert entry.message in window.operation_log.model.index(0, 0).data(Qt.ItemDataRole.ToolTipRole)
    window.operation_log.copy_all_action.trigger()
    assert entry.message in QApplication.clipboard().text()


def test_batch_text_changes_keep_one_log_and_transaction_endpoint_values(log_window, tmp_path):
    """一次事务只记一条，忽略中间草稿，按既有摘要上限展示三组并提示剩余组数。"""
    window = log_window
    engine = create(window, tmp_path / "批量改字.mindmap").engine
    keys = [engine.add_child(engine.root_id, name + "旧") for name in ("甲", "乙", "丙", "丁")]
    window.operation_log.clear()
    with engine.transaction("批量修改文字"):
        engine.update_node(keys[0], text="中间状态")
        for key, name in zip(keys, ("甲", "乙", "丙", "丁")):
            engine.update_node(key, text=name + "新")
    expected = "批量修改文字：从「甲旧」改为「甲新」；从「乙旧」改为「乙新」；从「丙旧」改为「丙新」；另有1组文字修改"
    assert len(entries(window)) == 1 and window.operation_log.model.entries[0].message == expected
    assert engine.undo()
    assert "撤销：批量修改文字：从「甲新」改为「甲旧」" in window.operation_log.model.entries[-1].message


def test_linked_page_name_sync_does_not_duplicate_text_change_description(log_window, tmp_path):
    """一个入口改名会同步子导图和详情页名称，日志仍只展示一次相同的前后内容。"""
    window = log_window
    engine = create(window, tmp_path / "名称同步.mindmap").engine
    owner = engine.add_child(engine.root_id, "哲学")
    submap = engine.create_submap(owner)
    detail = engine.create_detail_page(owner)
    roots = [engine.document.page_record(page).root_id for page in (submap, detail)]
    window.operation_log.clear()
    engine.update_node(owner, text="哲学导论")
    assert len(entries(window)) == 1
    assert window.operation_log.model.entries[0].message == "修改节点文字：从「哲学」改为「哲学导论」"
    assert all(engine.get_node(key).text == "哲学导论" for key in (owner, *roots))
    assert engine.undo()
    assert window.operation_log.model.entries[-1].message == "撤销：修改节点文字：从「哲学导论」改为「哲学」"


def test_text_logs_ignore_unchanged_text_and_keep_style_change_summary(log_window, tmp_path):
    window = log_window
    engine = create(window, tmp_path / "文字未变.mindmap").engine
    key = engine.add_child(engine.root_id, "哲学")
    window.operation_log.clear()
    engine.update_node(key, text="哲学")
    assert entries(window) == []
    engine.update_node(key, style={"font_size": 20})
    assert len(entries(window)) == 1
    message = window.operation_log.model.entries[0].message
    assert message.startswith("修改节点属性：") and "改为" not in message


def test_text_log_snapshot_survives_later_changes_and_deletion(log_window, tmp_path):
    window = log_window
    engine = create(window, tmp_path / "文字快照.mindmap").engine
    key = engine.add_child(engine.root_id, "哲学")
    events = []
    engine.subscribe(events.append)
    window.operation_log.clear()
    engine.update_node(key, text="哲学导论")
    original = events[-1]
    engine.update_node(key, text="后续内容")
    engine.remove_nodes([key])
    expected = "修改节点文字：从「哲学」改为「哲学导论」"
    assert describe_change(original) == expected and window.operation_log.model.entries[0].message == expected


def test_drag_log_names_actual_node_and_both_parents_with_undo_redo(log_window, tmp_path):
    """移动节点不在实体差量中时仍输出准确对象，撤销及重做交换实际来源和去向。"""
    window = log_window
    engine = create(window, tmp_path / "移动日志.mindmap").engine
    engine.update_node(engine.root_id, text="第二大脑")
    engine.set_layout(LayoutConfig(direction="right"))
    source = engine.add_child(engine.root_id, "哲学")
    target = engine.add_child(engine.root_id, "07.人文学科")
    engine.replace_document(engine.document)
    events = []
    engine.subscribe(events.append)
    window.operation_log.clear()
    with engine.transaction("拖动节点"):
        engine.move_nodes([source], target)
    message = "拖动节点：将「哲学」从「第二大脑」移到「07.人文学科」下"
    assert source not in events[-1].node_ids
    assert len(entries(window)) == 1 and window.operation_log.model.entries[0].message == message
    assert window.operation_log.model.entries[0].display_parts == ("拖动节点", message.split("：", 1)[1])
    assert engine.undo()
    assert window.operation_log.model.entries[-1].message == "撤销：拖动节点：将「哲学」从「07.人文学科」移回「第二大脑」下"
    assert engine.redo()
    assert window.operation_log.model.entries[-1].message == "重做：" + message
    assert len(entries(window)) == 3
    assert window.operation_log.model.entries[0].message == message


def test_same_parent_sort_reports_only_dragged_node_and_one_based_positions(log_window, tmp_path):
    window = log_window
    engine = create(window, tmp_path / "排序.mindmap").engine
    engine.update_node(engine.root_id, text="人文学科")
    engine.set_layout(LayoutConfig(direction="right"))
    a, _, c = [engine.add_child(engine.root_id, name) for name in ("艺术", "历史", "哲学")]
    engine.replace_document(engine.document)
    window.operation_log.clear()
    with engine.transaction("拖动节点"):
        engine.move_nodes([c], a, "before")
    assert window.operation_log.model.entries[-1].message == "拖动节点：「哲学」在「人文学科」下由第3项调整为第1项"
    assert engine.undo()
    assert window.operation_log.model.entries[-1].message == "撤销：拖动节点：「哲学」在「人文学科」下由第1项调整为第3项"


@pytest.mark.parametrize("reordered", [False, True])
def test_side_change_log_includes_sort_when_needed(log_window, tmp_path, reordered):
    window = log_window
    engine = create(window, tmp_path / "换边.mindmap").engine
    engine.update_node(engine.root_id, text="第二大脑")
    engine.set_layout(LayoutConfig(direction="both"))
    a, b, c = [engine.add_child(engine.root_id, name) for name in ("艺术", "历史", "哲学")]
    for key, side in ((a, "right"), (b, "left"), (c, "right")):
        engine.update_node(key, direction=side)
    engine.replace_document(engine.document)
    window.operation_log.clear()
    with engine.transaction("拖动节点"):
        engine.move_nodes([c if reordered else a], b, "before")
    detail = "「哲学」在「第二大脑」下由第3项调整为第2项，从右侧移到左侧" if reordered else "「艺术」在「第二大脑」下从右侧移到左侧"
    assert window.operation_log.model.entries[-1].message == "拖动节点：" + detail
    assert engine.undo()
    assert "从左侧移到右侧" in window.operation_log.model.entries[-1].message
    assert ("由第2项调整为第3项" in window.operation_log.model.entries[-1].message) is reordered


def test_multi_drag_logs_each_root_parent_once_and_excludes_descendants(log_window, tmp_path):
    window = log_window
    engine = create(window, tmp_path / "批量拖动.mindmap").engine
    engine.set_layout(LayoutConfig(direction="right"))
    a, b, target = [engine.add_child(engine.root_id, name) for name in ("知识库", "收藏", "人文学科")]
    first = engine.add_child(a, "哲学")
    second = engine.add_child(b, "艺术")
    child = engine.add_child(first, "随行后代")
    engine.replace_document(engine.document)
    window.operation_log.clear()
    with engine.transaction("拖动节点"):
        engine.move_nodes([second, first, child], target)
    expected = "拖动节点：将「哲学」从「知识库」移到「人文学科」下；将「艺术」从「收藏」移到「人文学科」下"
    assert len(entries(window)) == 1 and window.operation_log.model.entries[0].message == expected
    assert "随行后代" not in window.operation_log.plain_text()
    window.operation_log.copy_all_action.trigger()
    assert expected in QApplication.clipboard().text()


def test_move_names_are_frozen_and_legacy_events_keep_generic_summary(log_window, tmp_path):
    window = log_window
    engine = create(window, tmp_path / "快照.mindmap").engine
    engine.update_node(engine.root_id, text="第二大脑")
    source = engine.add_child(engine.root_id, "哲学")
    target = engine.add_child(engine.root_id, "人文学科")
    events = []
    engine.subscribe(events.append)
    with engine.transaction("拖动节点"):
        engine.move_nodes([source], target)
    event = events[-1]
    expected = "拖动节点：将「哲学」从「第二大脑」移到「人文学科」下"
    engine.update_node(target, text="新父级名称")
    engine.remove_nodes([source])
    assert describe_change(event) == expected
    event.changes.moves = ()
    fallback = describe_change(event)
    assert fallback.startswith("拖动节点：") and "移到" not in fallback


def test_unnamed_move_nodes_and_parents_have_stable_short_identifiers(log_window, tmp_path):
    window = log_window
    engine = create(window, tmp_path / "无文字.mindmap").engine
    engine.update_node(engine.root_id, text="第二大脑")
    source = engine.add_child(engine.root_id, "")
    target = engine.add_child(engine.root_id, "")
    window.operation_log.clear()
    with engine.transaction("拖动节点"):
        engine.move_nodes([source], target)
    expected = f"拖动节点：将「无文字节点（{source[:8]}）」从「第二大脑」移到「无文字节点（{target[:8]}）」下"
    assert window.operation_log.model.entries[0].message == expected


@pytest.mark.parametrize(("operation", "label"), [
    ("text", "修改节点文字"), ("style", "修改节点属性"), ("branch", "修改分支样式"),
    ("layout", "修改导图布局"), ("theme", "修改导图主题"), ("fold", "调整节点折叠状态"),
    ("move", "移动节点"), ("delete", "删除节点"), ("paste", "粘贴节点"),
    ("submap", "创建子导图"), ("detail", "创建详情页"), ("image", "新增图片节点"),
])
def test_edit_operations_have_chinese_summaries(log_window, tmp_path, operation, label):
    """不同编辑通道均从实际提交取得一次摘要，避免只记录菜单点击或原始英文操作名。"""
    window = log_window
    engine = create(window, tmp_path / "操作.mindmap").engine
    child = engine.add_child(engine.root_id, "待操作主题")
    engine.add_child(child, "后代")
    leaf = engine.add_child(engine.root_id, "叶子")
    window.operation_log.clear_action.trigger()
    operations = {
        "text": lambda: engine.update_node(child, text="改名后的主题"),
        "style": lambda: engine.update_node(child, style={"font_size": 22}),
        "branch": lambda: engine.set_branch_style([child], BranchStyle.LINE),
        "layout": lambda: engine.set_layout(LayoutConfig(direction="down")),
        "theme": lambda: engine.set_theme(Theme.dark()),
        "fold": lambda: engine.set_expanded(child, False),
        "move": lambda: engine.move_nodes([leaf], child),
        "delete": lambda: engine.remove_nodes([child]),
        "paste": lambda: engine.paste_fragment(engine.root_id, engine.copy_fragment([leaf])),
        "submap": lambda: engine.create_submap(leaf),
        "detail": lambda: engine.create_detail_page(leaf),
    }
    if operation == "image":
        image = QImage(8, 8, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.red)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        assert image.save(buffer, "PNG")
        engine.add_image_child(engine.root_id, bytes(buffer.data()), width=8)
    else:
        operations[operation]()
    assert len(entries(window)) == 1 and label in entries(window)[0]


def test_new_open_duplicate_close_and_cancel_follow_results(log_window, tmp_path, monkeypatch):
    """文件成功才记结果，重复打开不伪装重载，所有对话框取消都保持安静。"""
    window = log_window
    path = tmp_path / "文件.mindmap"
    session = create(window, path)
    assert len(entries(window)) == 1 and "新建导图" in entries(window)[0]
    assert window.open_path(path) and window.application.active_session is session
    assert len(entries(window)) == 2 and "激活已打开" in entries(window)[1]
    assert window.manager.close_document(session.id)
    assert len(entries(window)) == 3 and "关闭导图" in entries(window)[2]
    assert window.operation_log.isVisible() and window.open_path(path)
    assert len(entries(window)) == 4 and "打开导图" in entries(window)[3]
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    assert not window.new_document() and not window.open_dialog() and not window.save(save_as=True)
    window.application.files.export_dialog("png")
    assert len(entries(window)) == 4


def test_errors_are_once_per_attempt_and_keep_source(log_window, tmp_path, monkeypatch):
    """业务和 UI 两层异常边界只记一次，相同失败的下一次尝试仍正常记录。"""
    window = log_window
    create(window, tmp_path / "当前导图.mindmap")
    window.operation_log.clear_action.trigger()
    missing = tmp_path / "缺失文件.mindmap"
    for _ in range(2):
        assert window.call(lambda: window.open_path(missing)) is None
    assert len(entries(window)) == 2
    assert all("[错误] [缺失文件.mindmap] 打开导图失败" in line for line in entries(window))
    assert missing.name not in window.application.files.path.name

    def fail(*args, **kwargs):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(sessions_module, "save_document", fail)
    assert not window.new_document(tmp_path / "创建失败.mindmap")
    assert len(entries(window)) == 3 and "[创建失败.mindmap]" in entries(window)[-1]
    assert "[错误]" in entries(window)[-1] and "新建导图：" not in entries(window)[-1]


def test_manual_save_autosave_save_all_and_old_file_names(log_window, tmp_path, monkeypatch):
    """自动保存不刷成功日志，手动请求记结果，另存为不回写旧记录的文件名。"""
    window = log_window
    session = create(window, tmp_path / "原名称.mindmap")
    window.operation_log.clear_action.trigger()
    session.engine.update_node(session.engine.root_id, text="已提交修改")
    assert window.operation_log.model.entries[0].source_path == str(tmp_path / "原名称.mindmap")
    assert session.document.autosave() and len(entries(window)) == 1
    assert window.save() and len(entries(window)) == 2 and "保存导图：" in entries(window)[-1]
    target = tmp_path / "新名称.mindmap"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), ""))
    assert window.save(save_as=True)
    assert "[原名称.mindmap]" in entries(window)[0] and "[新名称.mindmap] 另存为" in entries(window)[-1]
    assert window.operation_log.model.entries[0].source_path == str(tmp_path / "原名称.mindmap")
    assert window.operation_log.model.entries[-1].source_path == str(target)
    window.operation_log.clear_action.trigger()
    assert window.save_all() and len(entries(window)) == 1 and "保存全部完成" in entries(window)[0]


@pytest.mark.parametrize("operation", ["save", "save_all", "close"])
def test_manual_save_and_close_failure_emit_one_error(log_window, tmp_path, operation):
    """关闭检查附带的取消提示只留在状态栏，不能把同一次写入失败再记一条日志。"""
    window = log_window
    session = create(window, tmp_path / "保存失败.mindmap")
    original = session.files.path.read_bytes()
    session.engine.update_node(session.engine.root_id, text="保留未保存内容")
    session.document.timer.stop()
    window.operation_log.clear_action.trigger()

    def fail(*args, **kwargs):
        raise OSError("模拟写入失败")

    session.files.writer = fail
    if operation == "close":
        assert not window.manager.close_document(session.id)
    else:
        window.action("app." + operation).trigger()
    assert len(entries(window)) == 1 and "[错误] [保存失败.mindmap]" in entries(window)[0]
    assert session.engine.is_dirty and session.files.path.read_bytes() == original
    assert not session.disposed and window.application.active_session is session


def test_view_navigation_selection_and_switches_do_not_log(log_window, tmp_path):
    """纯浏览不记录；后台会话的编辑独立记录来源，不切换当前活动文件。"""
    window = log_window
    first = create(window, tmp_path / "甲.mindmap")
    node = first.engine.add_child(first.engine.root_id, "子导图入口")
    page = first.engine.create_submap(node)
    second = create(window, tmp_path / "乙.mindmap")
    window.operation_log.clear_action.trigger()
    first.engine.update_node(first.engine.root_id, text="后台主题修改")
    assert len(entries(window)) == 1 and "[甲.mindmap]" in entries(window)[0]
    assert window.application.active_session is second
    window.operation_log.clear_action.trigger()
    second.canvas.select_nodes([second.engine.root_id])
    second.canvas.view.zoom(1.2)
    second.canvas.view.centerOn(0, 0)
    second.canvas.set_wheel_mode("zoom")
    window.manager.activate_document(first.id)
    first.canvas.navigate_to(page)
    first.canvas.navigate_to(None)
    window.manager.toggle("properties")
    window.manager.toggle("operation_log")
    assert entries(window) == []


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_background_document_save_logs_only_failure_with_own_source(
    log_window, tmp_path, monkeypatch, background, failed,
):
    """后台标签同步或异步保存成功不记录，失败仅一条且不激活标签或改变焦点。"""
    window = log_window
    first = create(window, tmp_path / "后台导图.mindmap")
    second = create(window, tmp_path / "活动导图.mindmap")
    first.engine.update_node(first.engine.root_id, text="后台修改")
    first.document.timer.stop()
    window.operation_log.clear_action.trigger()
    second.canvas.view.setFocus()
    focus = QApplication.focusWidget()
    if background:
        future = Future()
        monkeypatch.setattr(sessions_module, "BACKGROUND_SAVE_NODE_LIMIT", 0)
        monkeypatch.setattr(sessions_module, "_save_pool", lambda: SimpleNamespace(submit=lambda *args, **kw: future))
        assert first.document.autosave(background=True)
        assert entries(window) == []
        if failed:
            future.set_exception(OSError("后台写入失败"))
        else:
            future.set_result(None)
        assert first.document._finish_background_save() is not failed
        assert first.document._finish_background_save()
    else:
        if failed:
            def fail(*args, **kwargs):
                raise OSError("同步写入失败")
            first.files.writer = fail
        assert first.document.autosave() is not failed
    assert window.application.active_session is second and QApplication.focusWidget() is focus
    assert len(entries(window)) == (1 if failed else 0)
    if failed:
        assert "[错误] [后台导图.mindmap]" in entries(window)[0]
        assert "活动导图.mindmap" not in entries(window)[0]


def test_canvas_errors_and_disposed_session_signals_do_not_leak(log_window, tmp_path):
    """编辑失败不污染保存状态，关闭后断开信号和引擎订阅，迟到结果不能追加日志。"""
    window = log_window
    session = create(window, tmp_path / "释放.mindmap")
    window.operation_log.clear_action.trigger()
    session.canvas.call(lambda: session.engine.remove_nodes([session.engine.root_id]))
    assert len(entries(window)) == 1 and "[错误] [释放.mindmap]" in entries(window)[0]
    assert not session.document.last_error and not session.files.is_dirty
    assert window.manager.close_document(session.id)
    count = len(entries(window))
    session.canvas.error_occurred.emit("迟到的编辑错误")
    session.files.error_occurred.emit("迟到的文件错误")
    session.files.message.emit("迟到的导出结果")
    session.engine.add_child(session.engine.root_id, "关闭后的外部引擎调用")
    assert len(entries(window)) == count


@pytest.mark.parametrize("format_id", ["png", "svg", "pdf", "txt"])
def test_export_success_and_failure_are_logged_once(log_window, tmp_path, monkeypatch, format_id):
    """从真实菜单验证导出结果、错误日志、原子替换失败及取消。"""
    window = log_window
    create(window, tmp_path / "导出源.mindmap")
    window.operation_log.clear_action.trigger()
    target = tmp_path / f"导出.{format_id}"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), ""))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    menu = window.main_menu()
    files = next(action.menu() for action in menu.actions() if action.text() == "文件")
    exports = next(action.menu() for action in files.actions() if action.text() == "导出当前页")
    action = next(action for action in exports.actions() if action.data() == format_id)
    action.trigger()
    assert target.is_file() and len(entries(window)) == 1
    assert "[信息] [导出源.mindmap] 已导出：" in entries(window)[0]

    def fail_replace(source, destination):
        assert destination == target
        raise OSError("模拟写入失败")

    original = target.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr("os.replace", fail_replace)
        action.trigger()
    assert target.read_bytes() == original and not list(tmp_path.glob("*.tmp"))
    assert len(entries(window)) == 2 and "[错误] [导出源.mindmap]" in entries(window)[-1]
    assert "模拟写入失败" in window.statusBar().currentMessage()

    wrong = tmp_path / "错误后缀.xyz"
    wrong.write_bytes(b"existing")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(wrong), ""))
    action.trigger()
    assert len(entries(window)) == 3 and "[错误] [导出源.mindmap]" in entries(window)[-1]
    assert wrong.read_bytes() == b"existing" and not wrong.with_suffix("." + format_id).exists()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    action.trigger()
    assert len(entries(window)) == 3
    menu.deleteLater()
