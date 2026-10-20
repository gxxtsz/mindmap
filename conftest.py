"""默认以离屏 Qt 执行自动化测试；需要桌面测试时可显式设置 QT_QPA_PLATFORM。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
