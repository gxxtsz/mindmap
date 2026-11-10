"""内置主题与局部样式；构造管理器绝不修改 QApplication 的全局外观。"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from .icons import IconProvider, IconRegistry

RESOURCE_ROOT = Path(__file__).parent / "resources"


def parse_color(value: str) -> QColor:
    """主题八位色统一采用 #RRGGBBAA；显式拆分以消除与 Qt 字符串规则的歧义。"""
    if isinstance(value, str) and value.startswith("#") and len(value) == 9:
        return QColor(*(int(value[n:n + 2], 16) for n in (1, 3, 5, 7)))
    return QColor(value)


@dataclass(frozen=True)
class ThemeTokens:
    """业务主题覆盖项。light/dark 为颜色键映射，metrics 为逻辑像素尺寸。

    未覆盖的键继承内置默认值；配置由应用代码提供，不从布局文件执行加载。
    字典按值复制到 Theme，调用方后续修改原字典不会悄悄改变已经显示的窗口。
    """

    light: dict[str, str] = field(default_factory=dict)
    dark: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, int] = field(default_factory=dict)


class Theme:
    """局部主题对象；图标供应器及令牌在主题和紧凑模式切换后继续保留。"""

    def __init__(self, name="light", compact=False, *, tokens=None, icons=None, font=None):
        if name not in ("light", "dark"):
            raise ValueError("主题只能是 light 或 dark")
        self.name = name
        self.dark = name == "dark"
        self.compact = compact
        supplied = tokens or ThemeTokens()
        self.tokens = ThemeTokens(dict(supplied.light), dict(supplied.dark), dict(supplied.metrics))
        self.icons: IconProvider = icons or IconRegistry()
        self.font = QFont(font) if font is not None else None
        self.data = json.loads((RESOURCE_ROOT / f"{name}.json").read_text("utf-8"))
        self.colors = dict(self.data["colors"])
        self.colors.update(self.tokens.dark if self.dark else self.tokens.light)
        if any(not parse_color(value).isValid() for value in self.colors.values()):
            raise ValueError("主题中存在无效颜色")
        self.metrics = dict(self.data["metrics"])
        self.metrics.update(self.tokens.metrics)
        if any(type(value) is not int or value < 0 or value > 1000 for value in self.metrics.values()):
            raise ValueError("主题尺寸必须为 0 至 1000 的整数")
        self.ui = {}

    def with_options(self, name=None, compact=None):
        """派生主题并保留业务的资源和字体；不改变其他管理器正在使用的对象。"""
        return Theme(name or self.name, self.compact if compact is None else compact,
                     tokens=self.tokens, icons=self.icons, font=self.font)

    def color(self, name, fallback=None):
        return parse_color(self.colors.get(name, fallback or "#68778D"))

    def css(self, name):
        color = self.color(name)
        return f"rgba({color.red()},{color.green()},{color.blue()},{color.alpha()})"

    @property
    def rail_width(self):
        return self.metrics["compact_rail_width" if self.compact else "rail_width"]

    @property
    def button_height(self):
        return self.metrics["compact_button_height" if self.compact else "button_height"]

    @property
    def gap(self):
        return self.metrics["compact_gap" if self.compact else "gap"]

    @property
    def radius(self):
        return self.metrics["radius"]

    @property
    def header_height(self):
        return self.metrics["compact_header_height" if self.compact else "header_height"]

    @property
    def editor_header_height(self):
        """编辑器标签栏单独收紧高度；工具标题尺寸和自定义主题既有度量保持独立。"""
        key = "compact_editor_header_height" if self.compact else "editor_header_height"
        return self.metrics.get(key, self.header_height)

    def icon(self, name: str | QIcon, size=20):
        """同时接受业务的 QIcon 和语义图标名，不要求引用组件包内部路径。"""
        if isinstance(name, QIcon):
            return QIcon(name)
        return self.icons.get_icon(name, color=self.color("text-muted").name(), size=size)

    def palette(self):
        palette = QPalette()
        for role, key in [
            (QPalette.ColorRole.Window, "main-window-bg"),
            (QPalette.ColorRole.WindowText, "text-default"),
            (QPalette.ColorRole.Base, "tool-window-bg"),
            (QPalette.ColorRole.AlternateBase, "toolbar-bg-hovered"),
            (QPalette.ColorRole.Text, "text-default"),
            (QPalette.ColorRole.Button, "tool-window-bg"),
            (QPalette.ColorRole.ButtonText, "text-default"),
            (QPalette.ColorRole.ToolTipBase, "tool-window-bg"),
            (QPalette.ColorRole.ToolTipText, "text-default"),
            (QPalette.ColorRole.Highlight, "toolbar-selected-bg-active"),
            (QPalette.ColorRole.HighlightedText, "text-default"),
            (QPalette.ColorRole.Link, "control-brand-border"),
        ]:
            palette.setColor(role, self.color(key))
        return palette

    def apply(self, widget: QWidget, extra_qss=""):
        """仅作用于明确指定的 QWidget 及其子控件，不接管宿主应用的全局字体或 QSS。"""
        if not isinstance(widget, QWidget):
            raise TypeError("局部主题必须应用到 QWidget；全局主题请显式调用 apply_global")
        widget.setPalette(self.palette())
        if self.font is not None:
            widget.setFont(self.font)
        widget.setStyleSheet(self.stylesheet() + extra_qss)

    def apply_global(self, app: QApplication):
        """显式启用全应用外观；仅供决定接管全部界面的应用调用。"""
        app.setPalette(self.palette())
        if self.font is not None:
            app.setFont(self.font)
        app.setStyleSheet(self.stylesheet())

    def stylesheet(self):
        bg, fg, muted = self.css("tool-window-bg"), self.css("text-default"), self.css("text-muted")
        hover, selected = self.css("toolbar-bg-hovered"), self.css("toolbar-selected-bg-active")
        border, accent = self.css("tool-window-border"), self.css("control-brand-border")
        return f"""
            QWidget {{ color:{fg}; selection-background-color:{selected}; selection-color:{fg}; }}
            QMainWindow,QDialog {{ background:{self.css('main-window-bg')}; }}
            QLabel {{ background:transparent; }}
            QScrollArea,QScrollArea > QWidget {{ background:{bg}; border:0; }}
            QScrollArea > QWidget > QWidget {{ background:{bg}; }}
            QMenu {{ background:{bg}; color:{fg}; border:1px solid {border}; padding:5px; }}
            QMenu::item {{ padding:7px 30px 7px 12px; border-radius:4px; }}
            QMenu::item:selected {{ background:{hover}; }}
            QMenu::item:disabled {{ color:{muted}; }}
            QMenu::separator {{ height:1px; background:{border}; margin:5px 8px; }}
            QTreeView,QListView,QTableView,QPlainTextEdit,QTextEdit {{ background:{bg}; color:{fg}; border:0; outline:0; }}
            QTreeView::item {{ height:{24 if self.compact else 28}px; padding-left:3px; }}
            QTreeView::item:selected,QListView::item:selected,QTableView::item:selected {{ background:{selected}; color:{fg}; }}
            QTreeView::item:hover,QListView::item:hover {{ background:{hover}; }}
            QHeaderView::section {{ background:{bg}; color:{muted}; border:0; border-bottom:1px solid {border}; padding:8px; }}
            QScrollBar:vertical {{ background:transparent; width:9px; margin:1px; }}
            QScrollBar:horizontal {{ background:transparent; height:9px; margin:1px; }}
            QScrollBar::handle:vertical,QScrollBar::handle:horizontal {{ background:{border}; border-radius:4px; min-height:24px; min-width:24px; }}
            QScrollBar::add-line,QScrollBar::sub-line {{ width:0;height:0; }}
            QScrollBar::add-page,QScrollBar::sub-page {{ background:transparent; }}
            QSplitter::handle {{ background:{self.css('main-window-bg')}; }}
            QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox {{ background:{bg}; color:{fg}; border:1px solid {border}; border-radius:5px; padding:6px; }}
            QLineEdit:focus {{ border:1px solid {accent}; }}
            QPushButton,QToolButton {{ background:{hover}; color:{fg}; border:0; border-radius:5px; padding:7px 12px; }}
            QPushButton:hover,QToolButton:hover {{ background:{selected}; }}
            QPushButton:disabled,QToolButton:disabled {{ color:{muted}; }}
            QTabWidget::pane {{ border:0; background:{bg}; }}
            QTabBar {{ background:transparent; }}
            QTabBar::tab {{ background:transparent; color:{muted}; padding:8px 12px; margin:4px 2px; border-radius:5px; }}
            QTabBar::tab:selected {{ background:{hover}; color:{fg}; }}
            QTabBar::tab:hover {{ background:{hover}; }}
            QStatusBar {{ background:{self.css('main-window-bg')}; color:{muted}; border:0; }}
            QStatusBar::item {{ border:0; }}
            QCheckBox {{ spacing:8px; background:transparent; }}
            QGroupBox {{ border:1px solid {border}; border-radius:6px; margin-top:16px; padding:12px; }}
            QGroupBox::title {{ subcontrol-origin:margin; left:12px; }}
        """
