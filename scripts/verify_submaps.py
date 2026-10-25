"""核验附属页面、收藏、图片辅助效果、紧凑模式与三档缩放，生成中文报告和截图。"""

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
    from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter
    from PySide6.QtTest import QTest
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
                    canvas.enter_submap(owner)
                    app.processEvents()
                    shared_root = canvas.document_view.root_id
                    shared_item = canvas.node_items[shared_root]
                    checks["子导图根显示共享详情图标"] = (
                        shared_item.detail_page_control.isVisible()
                        and shared_item.visual.page_kinds == ("detail",)
                        and shared_item.visual.page_icon_color == shared_item.visual.text_color
                        and engine.resolve_detail_page(shared_root) == (owner, detail))
                    assert window.grab().save(str(output / (label + "-shared-detail-root.png")))
                    canvas.enter_detail_page(shared_root)
                    checks["共享详情返回提示指向子导图"] = (
                        canvas.current_page_id == detail
                        and canvas.path_navigation.back_button.toolTip() == "返回所属子导图")
                    canvas.return_to_parent_page()
                    checks["共享详情返回原子导图"] = (
                        canvas.current_page_id == page and canvas.selected_node_ids() == [shared_root])
                    canvas.return_to_parent_page()
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
                    # 在真实编辑器中粘贴透明位图，核验快捷键、几何、主题及文件资源。
                    source = QImage(220, 140, QImage.Format.Format_ARGB32)
                    source.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(source)
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(35, 115, 220, 170))
                    painter.drawRoundedRect(QRectF(8, 8, 204, 124), 20, 20)
                    painter.setPen(QColor("#ffffff"))
                    painter.drawText(QRectF(8, 8, 204, 124), Qt.AlignmentFlag.AlignCenter, "剪贴板图片")
                    painter.end()
                    canvas.view.setFocus()
                    # 收藏导航恢复的旧选择可能是子导图入口；使用允许普通后代的父节点。
                    target = engine.add_child(canvas.document_view.root_id, "图片粘贴位置")
                    canvas.begin_edit(target)
                    canvas.editor.setPlainText("直接粘贴的透明图片")
                    mime = QMimeData()
                    mime.setImageData(source)
                    app.clipboard().setMimeData(mime)
                    image_scale = canvas.view.transform().m11()
                    QTest.keyClick(canvas.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
                    app.processEvents()
                    image_node = canvas.current_node_id()
                    node = engine.get_node(image_node)
                    visual = canvas.node_items[image_node].visual
                    checks["编辑期间粘贴提交正文"] = (canvas.editor is None
                        and engine.get_node(target).text == "直接粘贴的透明图片")
                    checks["图片新建为普通子节点"] = engine.parent_id(image_node) == target
                    checks["粘贴图片实际显示"] = bool(node.image_id) and visual.image is not None
                    checks["粘贴保持原始像素尺寸"] = (node.image_width == 220
                        and (visual.width, visual.height) == (220, 140))
                    checks["纯图片不显示文字及节点背景"] = visual.image_only and node.text == ""
                    checks["纯图片隐藏附属页面入口"] = (not canvas.action("create_submap").isVisible()
                        and not canvas.action("create_detail_page").isVisible())
                    checks["图片不超出节点边界"] = QRectF(0, 0, visual.width, visual.height).contains(visual.image_rect)
                    checks["粘贴保留页面及缩放"] = (canvas.current_page_id == nested
                                                     and canvas.view.transform().m11() == image_scale)
                    checks["透明图片保留透明度"] = QImage.fromData(
                        engine.document.assets[node.image_id].data).pixelColor(0, 0).alpha() == 0
                    checks["粘贴图片随文件保存"] = window.save_all() and (
                        load_document(window.files.path) == engine.document)
                    canvas.view.ensureVisible(canvas.node_items[image_node], 30, 30)
                    app.processEvents()
                    assert window.grab().save(str(output / (label + "-image-paste.png")))
                    item = canvas.node_items[image_node]
                    start = canvas.view.mapFromScene(item.mapToScene(visual.image_rect.bottomRight()))
                    end = start + QPoint(round(55 * image_scale), round(35 * image_scale))

                    def image_mouse(kind, point):
                        """通过视口发送真实鼠标事件，明确携带拖动按钮状态。"""
                        button = (Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove
                                  else Qt.MouseButton.LeftButton)
                        buttons = (Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease
                                   else Qt.MouseButton.LeftButton)
                        event = QMouseEvent(kind, QPointF(point),
                            QPointF(canvas.view.viewport().mapToGlobal(point)), button, buttons,
                            Qt.KeyboardModifier.NoModifier)
                        app.sendEvent(canvas.view.viewport(), event)

                    before_resize, revision = engine.document, engine.revision
                    image_mouse(QEvent.Type.MouseButtonPress, start)
                    image_mouse(QEvent.Type.MouseMove, end)
                    app.processEvents()
                    checks["拖动图片只预览"] = (canvas.view.image_resize.active
                        and engine.document == before_resize and engine.revision == revision)
                    assert window.grab().save(str(output / (label + "-image-resize.png")))
                    image_mouse(QEvent.Type.MouseButtonRelease, end)
                    checks["松手等比缩放并保留原资源"] = (
                        engine.get_node(image_node).image_width > 260
                        and engine.document.assets == before_resize.assets
                        and canvas.node_items[image_node].visual.width
                            / canvas.node_items[image_node].visual.height == 220 / 140)
                    resized = engine.document
                    checks["图片缩放独立撤销重做"] = (engine.undo() and engine.document == before_resize
                        and engine.redo() and engine.document == resized)
                    # 同色图片才是轻投影的目标场景，使用临时图片，不改写用户资源。
                    contrast = QImage(300, 160, QImage.Format.Format_ARGB32)
                    background = canvas.scene.backgroundBrush().color()
                    contrast.fill(background)
                    painter = QPainter(contrast)
                    painter.setPen(QColor(canvas._ui_colors.text))
                    font = painter.font()
                    font.setPointSizeF(15)
                    font.setBold(True)
                    painter.setFont(font)
                    painter.drawText(QRectF(24, 22, 252, 40), "与画布同色的图片")
                    font.setPointSizeF(10)
                    font.setBold(False)
                    painter.setFont(font)
                    painter.drawText(QRectF(24, 78, 252, 55), Qt.TextFlag.TextWordWrap,
                                     "轻投影提示图片边界，悬停出现细轮廓，选中后保留缩放手柄。")
                    painter.end()
                    contrast_path = Path(temporary) / (label + "-contrast.png")
                    assert contrast.save(str(contrast_path))
                    canvas.import_image(target, contrast_path)
                    contrast_node = canvas.current_node_id()
                    item = canvas.node_items[contrast_node]
                    canvas.select_nodes([])
                    canvas.view.centerOn(item)
                    canvas.view.update_cursor(QPoint(2, 2))
                    app.processEvents()
                    idle = canvas.view.viewport().grab()
                    assert window.grab().save(str(output / (label + "-image-shadow.png")))
                    sample = canvas.view.mapFromScene(item.mapToScene(QPointF(-2, 40)))
                    pixels, ratio = idle.toImage(), idle.devicePixelRatioF()
                    shadow_color = pixels.pixelColor(round(sample.x() * ratio), round(sample.y() * ratio))
                    checks["同色图片的外缘投影可见"] = (shadow_color.lightness() > background.lightness()
                        if theme == "dark" else shadow_color.lightness() < background.lightness())
                    before_hover, hover_revision = engine.document, engine.revision
                    center = canvas.view.mapFromScene(item.mapToScene(item.visual.image_rect.center()))
                    canvas.view.update_cursor(center)
                    app.processEvents()
                    checks["图片悬停显示轮廓"] = item.hovered and canvas.view.viewport().grab().toImage() != idle.toImage()
                    assert window.grab().save(str(output / (label + "-image-hover.png")))
                    canvas.view.update_cursor(QPoint(2, 2))
                    app.processEvents()
                    checks["移出图片恢复轻投影"] = not item.hovered and canvas.view.viewport().grab().toImage() == idle.toImage()
                    checks["投影与悬停不修改文档历史"] = engine.document == before_hover and engine.revision == hover_revision
                    canvas.select_nodes([contrast_node])
                    app.processEvents()
                    corner = canvas.view.mapFromScene(item.mapToScene(item.visual.image_rect.bottomRight()))
                    checks["同色图片选中后仍可缩放"] = canvas.view.image_resize.hit_test(corner) == 4
                    assert window.grab().save(str(output / (label + "-image-selected.png")))
                    app.clipboard().clear()
                    canvas.set_editable(False)
                    checks["只读隐藏缩放手柄"] = canvas.view.image_resize.hit_test(start) is None
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
    parser = argparse.ArgumentParser(description="附属页面、收藏与图片粘贴的三档缩放及主题视觉验收")
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
