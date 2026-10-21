"""公开图标入口的缓存、状态和非法名称验收。"""

import pytest
from mindmap_qt_ui import toolbar_icon
from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon


def test_public_toolbar_icon_has_all_states_and_checks_names(qapp):
    icon = toolbar_icon("outline", "#111111", "#888888", "#3478d5")
    assert icon is toolbar_icon("outline", "#111111", "#888888", "#3478d5")
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled, QIcon.Mode.Active):
        for state in (QIcon.State.Off, QIcon.State.On):
            assert not icon.pixmap(QSize(20, 20), mode, state).isNull()
    with pytest.raises(ValueError, match="图标不存在"):
        toolbar_icon("不存在", "#111111", "#888888", "#3478d5")
