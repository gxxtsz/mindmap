"""验收灵感侧栏、单想法画布及三档显示缩放，使用临时文件并生成中文报告。"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def child(scale):
    """每档缩放启动独立 Qt 进程，截图和报告不修改用户导图。"""
    from mindmap_formats import load_document
    from mindmap_workbench.shell_adapter import build_window
    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtGui import QContextMenuEvent, QPainter
    from PySide6.QtWidgets import QApplication, QDialog, QTreeWidget

    app = QApplication([])
    output = ROOT / ".artifacts" / "inspiration" / "offscreen" / f"{scale:g}x"
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    with TemporaryDirectory(prefix="mindmap-inspiration-") as directory:
        for theme in ("light", "dark"):
            for compact in (False, True):
                label = theme + ("-compact" if compact else "-normal")
                window = build_window(storage_dir=Path(directory) / label, theme=theme, restore=False)
                try:
                    window.manager.set_option("compact", compact)
                    window.resize(1280, 820)
                    window.show()
                    app.processEvents()
                    path = Path(directory) / (label + ".mindmap")
                    assert window.new_document(path)
                    canvas, engine = window.canvas, window.engine
                    canvas.show_inspiration()
                    app.processEvents()
                    assert window.grab().save(str(output / f"{label}-empty.png"))
                    window.active_session.inspiration.new_button.click()
                    a = canvas.current_inspiration_root_id
                    assert a is not None and canvas.editor is not None
                    canvas.finish_edit(commit=False)
                    engine.update_node(a, text="做一个读书工具")
                    engine.set_inspiration_positions({a: (-360, -100)})
                    first = engine.add_child(a, "摘录：先收集有价值的想法")
                    engine.add_child(a, "复习：稍后形成清晰的结构")
                    b = engine.add_inspiration("个人知识库", position=(260, 180))
                    detail = engine.create_detail_page(a)
                    engine.add_child(engine.document.detail_pages[detail].root_id, "实现思路与使用场景")
                    page = engine.create_submap(b)
                    page_child = engine.add_child(engine.document.submaps[page].root_id, "主题整理方法")
                    engine.set_submap_favorite(page, True)
                    canvas.show_inspiration(a)
                    app.processEvents()
                    panel = window.active_session.inspiration
                    viewport = panel.list_view.viewport()
                    point = panel.list_view.visualItemRect(panel.list_view.item(1)).center()
                    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, viewport.mapToGlobal(point))
                    QApplication.sendEvent(viewport, event)
                    app.processEvents()
                    menu = panel._menu
                    menu_valid = (event.isAccepted() and menu.isVisible()
                                  and [action.text() for action in menu.actions()] ==
                                  ["加入导图…", "", "删除想法"] and menu.defaultAction() is None
                                  and panel._menu_root == b and canvas.current_inspiration_root_id == a)
                    # 菜单是独立顶层控件；按真实屏幕相对位置合入窗口截图，覆盖各档缩放和主题。
                    preview = window.grab()
                    painter = QPainter(preview)
                    painter.drawPixmap(menu.mapToGlobal(QPoint()) - window.mapToGlobal(QPoint()), menu.grab())
                    painter.end()
                    assert preview.save(str(output / f"{label}-context-menu.png"))
                    panel.close_popups()
                    app.processEvents()
                    checks = {
                        "列表右键独立菜单且不提前切换想法": menu_valid,
                        "默认只显示当前想法": a in canvas.node_items and b not in canvas.node_items,
                        "单想法根在展示原点": canvas.layout_result.nodes[a].x == 0,
                        "侧栏单选列出全部想法": window.active_session.inspiration.list_view.count() == 2,
                        "灵感侧栏可见": window.active_session.inspiration.isVisible(),
                        "路径栏已移除重复按钮": (not hasattr(canvas.path_navigation, "main_button")
                                               and not hasattr(canvas.path_navigation, "new_inspiration_button")),
                        "想法返回提示正确": canvas.path_navigation.back_button.toolTip() == "返回主导图",
                    }
                    assert window.grab().save(str(output / f"{label}-single.png"))
                    canvas.center_root()
                    canvas.select_nodes([a])
                    app.processEvents()
                    checks.update({
                        "显示缩放正确": abs(window.devicePixelRatioF() - scale) < 0.01,
                        "已移除总览与区域入口": (not hasattr(window.active_session.inspiration, "overview_button")
                                               and not hasattr(canvas.path_navigation, "inspiration_button")),
                        "存储坐标完整保留": engine.document.inspiration.positions[a] == (-360, -100),
                        "大纲只含当前想法": window.panels.outline.tree_model.rowCount() == 1,
                        "路径只含想法名称": [entry.text for entry in canvas.path_navigation.entries] == ["做一个读书工具"],
                        "详情页与子导图共存": bool(engine.get_node(a).detail_page_id and engine.get_node(b).submap_id),
                    })
                    assert window.grab().save(str(output / f"{label}-navigation.png"))

                    def capture_dialog():
                        dialog = app.activeModalWidget()
                        if isinstance(dialog, QDialog):
                            tree = dialog.findChild(QTreeWidget)
                            tree.setCurrentItem(tree.topLevelItem(0))
                            app.processEvents()
                            dialog.grab().save(str(output / f"{label}-target.png"))
                            dialog.reject()

                    QTimer.singleShot(0, capture_dialog)
                    checks["目标选择可取消"] = canvas.choose_inspiration_target() is None
                    canvas.enter_detail_page(a)
                    checks["详情返回提示正确"] = canvas.path_navigation.back_button.toolTip() == "返回所属想法"
                    app.processEvents()
                    assert window.grab().save(str(output / f"{label}-detail.png"))
                    canvas.return_to_parent_page()
                    checks["详情返回所属想法"] = canvas.current_inspiration_root_id == a
                    original = engine.document
                    engine.attach_inspirations([a], engine.root_id)
                    checks["接入显示完整分支"] = canvas.current_page_id is None and first in canvas.node_items
                    checks["回主图仍保留灵感侧栏"] = window.active_session.inspiration.isVisible()
                    engine.undo()
                    checks["一次撤销恢复全部内容"] = engine.document == original
                    engine.redo()
                    canvas.select_nodes([a])
                    canvas.move_selected_to_inspiration()
                    grouped = engine.group_inspirations([a, b], position=(-200, 0))
                    checks["组合保留子导图收藏"] = (engine.parent_id(a) == grouped
                                                    and engine.is_submap_favorite(page)
                                                    and engine.get_node(page_child).text == "主题整理方法")
                    engine.undo()
                    canvas.set_editable(False)
                    checks["只读禁止修改允许浏览"] = (not canvas.action("group_inspirations").isEnabled()
                                                   and not window.active_session.inspiration.new_button.isEnabled())
                    canvas.navigate_to(detail)
                    checks["只读可查看详情"] = canvas.current_page_id == detail
                    canvas.set_editable(True)
                    canvas.show_inspiration()
                    window.active_session.flush()
                    checks["完整文件往返"] = load_document(path) == engine.document
                    canvas.export(output / f"{label}.svg")
                    exported = (output / f"{label}.svg").read_text(encoding="utf-8")
                    checks["单想法图形导出"] = "做一个读书工具" in exported and "个人知识库" not in exported
                    checks["导出使用想法标题"] = "<title>做一个读书工具</title>" in exported
                    reports.append({"主题": theme, "紧凑模式": compact, "显示缩放": scale,
                                    "检查": checks, "通过": all(checks.values())})
                finally:
                    window.close()
    (output / "report.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"显示缩放": scale, "通过": all(r["通过"] for r in reports), "组合数量": len(reports)},
                     ensure_ascii=False), flush=True)
    return 0 if all(r["通过"] for r in reports) else 1


def main():
    """运行三档离屏验收；子进程隐藏启动，失败时显示诊断信息。"""
    parser = argparse.ArgumentParser(description="独立想法界面验收")
    parser.add_argument("--child", type=float)
    parser.add_argument("--interactive", action="store_true", help="打开仅使用临时文件的原生鼠标验收窗口")
    options = parser.parse_args()
    if options.interactive:
        return interactive()
    if options.child:
        return child(options.child)
    passed = True
    for scale in (1, 1.5, 2):
        environment = os.environ.copy()
        environment.update(QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=str(scale), PYTHONIOENCODING="utf-8")
        result = subprocess.run(
            [sys.executable, __file__, "--child", str(scale)], cwd=ROOT, env=environment,
            capture_output=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        print(result.stdout.decode("utf-8", errors="replace").strip(), flush=True)
        passed &= result.returncode == 0
        if result.returncode:
            print(result.stderr.decode("utf-8", errors="replace")[-3000:])
    return 0 if passed else 1


def interactive():
    """用临时文件打开真实桌面窗口；中文现场报告只写入忽略的验收产物目录。

    供鼠标跨控件手势验收使用，不模拟原生拖放，不加载最近文件或用户导图。
    关闭窗口或十五分钟超时后正常释放会话与临时目录，不修改用户本机配置。
    """
    os.environ["QT_QPA_PLATFORM"] = "windows" if sys.platform == "win32" else "xcb"
    from mindmap_workbench.shell_adapter import build_window
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    output = ROOT / ".artifacts" / "inspiration" / "native"
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="mindmap-native-inspiration-") as directory:
        window = build_window(storage_dir=Path(directory) / "配置", restore=False)
        window.resize(1320, 860)
        assert window.new_document(Path(directory) / "灵感侧栏验收.mindmap")
        canvas, engine = window.canvas, window.engine
        engine.update_node(engine.root_id, text="项目规划")
        target = engine.add_child(engine.root_id, "主导图分支")
        engine.add_child(target, "完整内容")
        first = engine.add_inspiration("读书工具", position=(-400, 20))
        engine.add_child(first, "收集摘录")
        engine.add_child(first, "复习卡片")
        second = engine.add_inspiration("知识库", position=(400, 30))
        engine.add_child(second, "相关资料")
        engine.create_detail_page(second)
        engine.replace_document(engine.document)
        canvas.show_inspiration(first)
        window.active_session.flush()
        events = []

        def report():
            document = engine.document
            parents = document.parents()
            state = {"游离根": [document.nodes[key].text for key in document.inspiration.root_ids],
                     "节点父级": {node.text: document.nodes[parents[key]].text if key in parents else None
                                  for key, node in document.nodes.items()
                                  if document.page_kind(engine.node_page_id(key)) in ("main", "inspiration")},
                     "未保存": engine.is_dirty, "修订号": engine.revision, "历史事件": events}
            if not canvas._disposed:
                root = canvas.current_inspiration_root_id
                state.update({"页面": canvas.current_page_kind,
                              "当前想法": document.nodes[root].text if root else None,
                              "画布节点": [node.text for node in canvas.document_view.nodes.values()]})
            (output / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

        def changed(event):
            if event.kind in ("change", "undo", "redo"):
                events.append({"类型": event.kind, "操作": event.operation, "修订号": event.revision})
            QTimer.singleShot(0, report)

        unsubscribe = engine.subscribe(changed)
        timer = QTimer(window)
        timer.setInterval(500)
        timer.timeout.connect(report)
        timer.start()
        QTimer.singleShot(15 * 60 * 1000, window.close)
        # 临时配置没有窗口位置；显式放到主屏中央，避免无边框窗口默认落在屏幕外。
        screen = app.primaryScreen()
        if screen is not None:
            window.move(screen.availableGeometry().center() - window.rect().center())
        window.show()
        report()
        result = app.exec()
        unsubscribe()
        timer.stop()
        report()
        return result


if __name__ == "__main__":
    raise SystemExit(main())
