"""公开图标入口的缓存、状态和非法名称验收。"""

import pytest
from mindmap_qt_ui import toolbar_icon
from mindmap_qt_ui.icons import add_icon_action
from PySide6.QtCore import QCoreApplication, QEvent, QSize
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QToolBar, QWidget
from shiboken6 import isValid


def test_public_toolbar_icon_has_all_states_and_checks_names(qapp):
    icon = toolbar_icon("outline", "#111111", "#888888", "#3478d5")
    assert icon is toolbar_icon("outline", "#111111", "#888888", "#3478d5")
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled, QIcon.Mode.Active):
        for state in (QIcon.State.Off, QIcon.State.On):
            assert not icon.pixmap(QSize(20, 20), mode, state).isNull()
    with pytest.raises(ValueError, match="图标不存在"):
        toolbar_icon("不存在", "#111111", "#888888", "#3478d5")


@pytest.mark.parametrize("kind", ["main", "detail", "submap"])
def test_page_icons_support_themes_and_high_dpi_states(qapp, kind):
    """路径栏复用页面图标，明暗配色及多倍率资源均由公共工厂提供。"""
    for foreground, muted, accent in (("#17283e", "#637187", "#3479dc"),
                                      ("#e3eaf6", "#a5b3c9", "#83b2ff")):
        icon = toolbar_icon(kind, foreground, muted, accent)
        for ratio in (1.0, 1.5, 2.0):
            for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled, QIcon.Mode.Active):
                pixmap = icon.pixmap(QSize(16, 16), ratio, mode)
                assert not pixmap.isNull()
                assert pixmap.devicePixelRatio() == ratio


def test_toolbar_description_survives_button_removal_and_toolbar_disposal(qtbot):
    """动作可长于按钮和工具栏存活，迟到更新不得访问被删除的 Qt 对象。"""
    owner = QWidget()
    qtbot.addWidget(owner)
    toolbar = QToolBar(owner)
    action = QAction("测试动作", owner)
    add_icon_action(toolbar, action, "fit")
    button = toolbar.widgetForAction(action)
    action.setToolTip("第一次说明")
    assert button.toolTip() == "第一次说明"
    toolbar.removeAction(action)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(button)
    action.setToolTip("按钮已移除")
    toolbar.addAction(action)
    replacement = toolbar.widgetForAction(action)
    action.setToolTip("新的按钮说明")
    assert replacement.toolTip() == "新的按钮说明"
    toolbar.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(toolbar)
    action.setToolTip("工具栏已释放")
