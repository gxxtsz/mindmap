"""按真实能力归属迁移的组件与行为回归。"""





from mindmap_shell import ActionSpec, AppSpec, WorkbenchWindow
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QLineEdit, QToolButton
from shiboken6 import isValid


def test_workbench_keeps_brand_and_shared_business_actions(qapp, qtbot, tmp_path):
    calls = []
    window = WorkbenchWindow(AppSpec("custom-app", "定制业务软件", storage_dir=tmp_path,
                                    actions=(ActionSpec("business.add", "新增", lambda: calls.append(1),
                                                        icon="document", toolbar=True),)))
    qtbot.addWidget(window)
    window.show()
    assert window.spec.title == "定制业务软件"
    window.add_action(ActionSpec("business.late", "稍后加入", lambda: calls.append(2), toolbar=True))
    assert any(item.text() == "稍后加入" for item in window.title_bar.action_buttons)
    # 动态业务动作仍在窗口控制按钮之前，且复用菜单中的同一 QAction。
    bar = window.title_bar
    assert bar.box.itemAt(bar.box.count() - 1).layout() is bar.window_controls
    assert all(bar.box.indexOf(button) < bar.box.count() - 1 for button in bar.action_buttons)
    window.action("business.add").trigger()
    assert calls == [1]
    window.manager.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_navigation_slot_inserts_replaces_and_releases_owned_widgets(qtbot, tmp_path):
    """导航始终位于菜单之后，替换或清空释放旧控件，右侧动作及窗口按钮不被重排。"""
    window = WorkbenchWindow(AppSpec("navigation-test", "导航验收", storage_dir=tmp_path))
    qtbot.addWidget(window)
    window.show()
    first = QToolButton()
    first.setText("业务导航")
    window.set_title_bar_navigation(first)
    bar = window.title_bar
    assert first.parentWidget() is bar and first.isVisible()
    assert bar.box.indexOf(first) == bar.box.indexOf(bar.menu) + 1
    window.set_title_bar_navigation(first)
    assert isValid(first) and bar.navigation is first
    second = QLineEdit("替换导航")
    window.set_title_bar_navigation(second)
    assert not first.isVisible() and bar.navigation is second
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(first)
    window.set_title_bar_navigation(None)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(second) and bar.navigation is None
    assert bar.box.itemAt(bar.box.count() - 1).layout() is bar.window_controls
