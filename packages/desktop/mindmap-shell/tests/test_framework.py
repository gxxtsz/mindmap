"""按真实能力归属迁移的组件与行为回归。"""





from mindmap_shell import ActionSpec, AppSpec, WorkbenchWindow
from PySide6.QtCore import QCoreApplication, QEvent


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
