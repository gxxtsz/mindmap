"""核验附属页面、路径栏、收藏侧栏、紧凑模式与三档缩放，生成中文报告和截图。"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def child(scale, platform):
    """每档缩放使用独立 QApplication，避免运行中改变 Qt 缩放基线。"""
    from mindmap_core import MindMapError
    from mindmap_formats import load_document
    from mindmap_workbench.shell_adapter import build_window
    from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QRectF
    from PySide6.QtWidgets import QApplication, QToolButton

    app = QApplication([])
    output = ROOT / ".artifacts/submaps" / platform / f"{scale:g}x"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with TemporaryDirectory(prefix="mindmap-submap-") as temporary:
        for theme in ("light", "dark"):
            for compact in (False, True):
                label = theme + ("-compact" if compact else "-normal")
                window = build_window(storage_dir=Path(temporary) / label, theme=theme, restore=False)
                try:
                    window.manager.set_option("compact", compact)
                    window.resize(1360, 880)
                    window.show()
                    app.processEvents()
                    assert window.new_document(Path(temporary) / (label + ".mindmap"))
                    canvas, engine = window.canvas, window.engine
                    engine.update_node(engine.root_id, text="编程学习笔记")
                    branch = engine.add_child(engine.root_id, "数据类型")
                    owner = engine.add_child(branch, "整数与进制")
                    engine.add_child(engine.root_id, "算法与数据结构")
                    canvas.select_nodes([owner])
                    canvas.view.zoom(1.25)
                    canvas.view.update_navigation_rect(center=QPointF(180, 80))
                    center = canvas.view.mapToScene(canvas.view.viewport().rect()).boundingRect().center()
                    original_scale = canvas.view.transform().m11()
                    page = canvas.create_submap()
                    app.processEvents()
                    root = canvas.document_view.root_id
                    integer = engine.add_child(root, "整数的表示")
                    decimal = engine.add_child(root, "十进制整数")
                    engine.add_child(integer, "有符号与无符号")
                    engine.add_child(integer, "取值范围")
                    canvas.select_nodes([decimal])
                    app.processEvents()
                    assert window.grab().save(str(output / (label + "-page.png")))
                    nested = canvas.create_submap()
                    app.processEvents()
                    key = engine.add_child(canvas.document_view.root_id, "进制转换方法")
                    canvas.select_nodes([key])
                    app.processEvents()
                    expected = "编程学习笔记 › 整数与进制 › 十进制整数"
                    checks = {
                        "实际缩放正确": abs(window.devicePixelRatioF() - scale) < 0.01,
                        "页面与路径栏可见": canvas.path_navigation.isVisible()
                        and canvas.path_navigation.height() == 36,
                        "完整页面路径": canvas.path_navigation.full_path == expected,
                        "只显示当前页": len(canvas.document_view.nodes) == 2,
                        "侧栏属于当前页": window.panels.outline.tree_model.document.root_id
                        == canvas.document_view.root_id,
                        "同一文件标签": len(window.sessions) == 1,
                    }
                    assert window.grab().save(str(output / (label + "-nested.png")))
                    for index in range(12):
                        key = engine.add_child(key, f"详细知识点 {index + 1}")
                    canvas.select_nodes([key])
                    app.processEvents()
                    checks["普通分支不延长路径"] = canvas.path_navigation.full_path == expected
                    # 页面链才会触发路径省略；普通分支再深，也不能混入页面菜单。
                    targets = {entry.text: (entry.page_id, entry.node_id)
                               for entry in canvas.path_navigation.entries}
                    for index in range(12):
                        key = engine.add_child(canvas.document_view.root_id, f"进制转换专题 {index + 1}")
                        canvas.select_nodes([key])
                        child_page = canvas.create_submap()
                        targets[canvas.document_view.root.text] = (child_page, canvas.document_view.root_id)
                    deepest = canvas.current_page_id
                    app.processEvents()
                    overflow = next(button for button in canvas.path_navigation.findChildren(QToolButton)
                                    if button.isVisible() and button.text() == "…")
                    actions = overflow.menu().actions()
                    checks["长路径仅收起页面"] = bool(actions) and all(
                        entry.page_root for entry in canvas.path_navigation.entries)
                    checks["省略菜单保留页面类型图标"] = all(not action.icon().isNull() for action in actions)
                    assert window.grab().save(str(output / (label + "-long-path.png")))
                    target_page, target_root = targets[actions[0].text()]
                    actions[0].trigger()
                    checks["隐藏页面点击跳转"] = (canvas.current_page_id == target_page
                                            and canvas.selected_node_ids() == [target_root])
                    canvas.navigate_to(deepest)
                    assert window.save_all()
                    revision = engine.revision
                    canvas.navigate_to(nested)
                    canvas.return_to_parent_page()
                    checks["返回一级页面"] = canvas.current_page_id == page
                    canvas.return_to_parent_page()
                    restored = canvas.view.mapToScene(canvas.view.viewport().rect()).boundingRect().center()
                    checks["恢复原视图"] = (canvas.view.transform().m11() == original_scale
                                             and (center - restored).manhattanLength() < 3 / original_scale
                                             and canvas.selected_node_ids() == [owner])
                    checks["导航不修改内容"] = engine.revision == revision and not engine.is_dirty
                    checks["全部页面保存"] = load_document(window.files.path) == engine.document
                    checks["常驻页面图标"] = canvas.node_items[owner].submap_control.isVisible()
                    assert window.grab().save(str(output / (label + "-entry.png")))
                    # 在实际工作台的主题、紧凑模式和缩放下验收原地编辑；只修改
                    # 临时草稿，取消后页面名称、子图同步关系、布局及保存标记不变。
                    original_layout = canvas.layout_result
                    original_visual = canvas.node_items[owner].visual
                    window.activateWindow()
                    canvas.begin_edit(owner)
                    canvas.editor.setPlainText("新节点")
                    app.processEvents()
                    short_visual = canvas.node_items[owner].visual
                    # 内嵌页面入口是实际内容；正文原来的宽度规则仍保持不变。
                    checks["短文字编辑仅增加页面入口宽度"] = (
                        canvas.editor.textWidth() < 100
                        and short_visual.width == canvas.editor.textWidth() + 28 + short_visual.page_controls_width)
                    assert window.grab().save(str(output / (label + "-editing-short.png")))
                    canvas.editor.setPlainText("长文本在节点内部自动换行，圆角高亮与连接线随内容调整。" * 6)
                    app.processEvents()
                    item, editor = canvas.node_items[owner], canvas.editor
                    visual = item.visual
                    checks["长文字自动换行"] = (editor.textWidth() == 280
                                                 and editor.document().begin().layout().lineCount() > 1)
                    checks["编辑外框包含正文"] = (item.sceneBoundingRect().contains(editor.sceneBoundingRect())
                                                    and visual.height > short_visual.height)
                    checks["编辑保持节点高亮"] = item.editing and item.isSelected()
                    checks["编辑外框保持屏内可见"] = canvas.view.mapToScene(
                        canvas.view.viewport().rect()).boundingRect().contains(item.sceneBoundingRect())
                    assert window.grab().save(str(output / (label + "-editing-wrapped.png")))
                    canvas.finish_edit(commit=False)
                    checks["取消编辑恢复正式尺寸"] = (canvas.layout_result is original_layout
                                                        and canvas.node_items[owner].visual is original_visual
                                                        and engine.revision == revision and not engine.is_dirty)
                    # 同一入口同时拥有两类页面，非叶子与主根也能附加详情；检查独立命中区。
                    detail = engine.create_detail_page(owner)
                    branch_detail = engine.create_detail_page(branch)
                    engine.create_detail_page(engine.root_id)
                    app.processEvents()
                    item = canvas.node_items[owner]
                    checks["两类入口形状与提示区分"] = (
                        item.detail_page_control.toolTip() == "查看详情页"
                        and item.submap_control.toolTip() == "进入子导图")
                    checks["两类入口互不重叠"] = not item.detail_page_control.boundingRect().intersects(
                        item.submap_control.boundingRect())
                    checks["页面入口全部位于节点内部"] = all(
                        QRectF(0, 0, node_item.visual.width, node_item.visual.height).contains(control.boundingRect())
                        for node_item in canvas.node_items.values()
                        for control in (node_item.detail_page_control, node_item.submap_control)
                        if control.isVisible())
                    item = canvas.node_items[branch]
                    checks["详情入口避让折叠按钮"] = not item.detail_page_control.boundingRect().intersects(
                        item.fold_control.button_rect())
                    assert window.grab().save(str(output / (label + "-detail-entry.png")))
                    canvas.enter_detail_page(owner)
                    engine.add_child(canvas.document_view.root_id, "业务规则与补充说明")
                    app.processEvents()
                    checks["详情路径有明确标识"] = canvas.path_navigation.full_path.endswith("整数与进制 · 详情")
                    page_buttons = [button for button in canvas.path_navigation.findChildren(QToolButton)
                                    if button.isVisible() and button.property("page_kind")]
                    checks["面包屑图标匹配页面类型"] = (
                        [button.property("page_kind") for button in page_buttons] == ["main", "detail"]
                        and all(not button.icon().isNull() for button in page_buttons))
                    checks["详情页隐藏创建入口"] = not canvas.action("create_submap").isVisible() and not (
                        canvas.action("create_detail_page").isVisible())
                    rejected = 0
                    for create in (engine.create_submap, engine.create_detail_page):
                        try:
                            create(canvas.document_view.root_id)
                        except MindMapError:
                            rejected += 1
                    checks["详情底层禁止两类附页"] = rejected == 2
                    checks["详情页侧栏隔离"] = window.panels.outline.tree_model.document.root_id == (
                        canvas.document_view.root_id)
                    assert window.grab().save(str(output / (label + "-detail-page.png")))
                    canvas.return_to_parent_page()
                    canvas.enter_detail_page(branch)
                    checks["非叶子详情可进入"] = canvas.current_page_id == branch_detail
                    canvas.navigate_to(detail)
                    checks["详情内容和完整文件可保存"] = window.save_all() and (
                        load_document(window.files.path) == engine.document)
                    canvas.navigate_to(nested)
                    canvas.set_submap_favorite(page, True)
                    canvas.set_submap_favorite(nested, True)
                    window.manager.activate("favorites", focus=False)
                    app.processEvents()
                    favorites = window.active_session.favorites
                    checks["收藏按最近时间排序"] = [row[0].page_id for row in favorites.model.rows] == [nested, page]
                    checks["当前子导图显示已收藏星标"] = (
                        canvas.path_navigation.favorite_button.isVisible()
                        and canvas.path_navigation.favorite_button.isChecked())
                    checks["收藏侧栏属于当前会话"] = (
                        favorites.isVisible() and window._favorites_stack.currentWidget() is favorites)
                    checks["收藏完整页面路径"] = favorites.model.rows[0][1] == expected
                    assert window.grab().save(str(output / (label + "-favorites.png")))
                    canvas.navigate_to(None)
                    favorites.list_view.clicked.emit(favorites.model.index(0, 0))
                    checks["点击收藏进入目标页"] = canvas.current_page_id == nested
                    checks["收藏保存在原生文件"] = window.save_all() and (
                        load_document(window.files.path).metadata["submap_favorites"] == [nested, page])
                    canvas.set_editable(False)
                    checks["只读禁用收藏修改"] = (
                        not canvas.path_navigation.favorite_button.isEnabled()
                        and not favorites.remove_action.isEnabled())
                    canvas.navigate_to(None, branch)
                    favorites.list_view.clicked.emit(favorites.model.index(0, 0))
                    checks["只读可通过收藏跳转"] = canvas.current_page_id == nested
                    canvas.navigate_to(None, branch)
                    checks["只读路径跳转"] = canvas.current_page_id is None
                    results.append({"主题": theme, "紧凑模式": compact, "缩放": scale,
                                    "检查": checks, "通过": all(checks.values())})
                finally:
                    window.close()
                    window.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    app.processEvents()
    (output / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(item["通过"] for item in results) else 1


def main():
    parser = argparse.ArgumentParser(description="子导图与详情页三档缩放及主题视觉验收")
    parser.add_argument("--child", type=float)
    parser.add_argument("--platform", default="offscreen")
    options = parser.parse_args()
    if options.child:
        return child(options.child, options.platform)
    passed = True
    for scale in (1, 1.5, 2):
        environment = dict(os.environ, QT_QPA_PLATFORM=options.platform, QT_SCALE_FACTOR=str(scale),
                           QT_SCALE_FACTOR_ROUNDING_POLICY="PassThrough", PYTHONIOENCODING="utf-8")
        environment.pop("QT_SCREEN_SCALE_FACTORS", None)
        result = subprocess.run([sys.executable, __file__, "--child", str(scale), "--platform", options.platform],
                                cwd=ROOT, env=environment, capture_output=True, timeout=90,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        passed &= result.returncode == 0
        print(f"{options.platform} / {scale:g}x：{'通过' if result.returncode == 0 else '失败'}", flush=True)
        if result.returncode:
            print(result.stderr.decode("utf-8", errors="replace")[-3000:])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
