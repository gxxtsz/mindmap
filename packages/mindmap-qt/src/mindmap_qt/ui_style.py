"""卡片工作区内部的视觉参数；界面配色与导图场景、文件主题严格分开。

这里的中性色参照界面截图独立设定，不是从 PyCharm 提取的资源。所有尺寸使用
Qt 逻辑像素，所有样式只施加到调用方拥有的控件，不修改 QApplication 的全局样式。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from mindmap_engine import Theme
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QComboBox, QStyle, QStyleOptionComboBox, QVBoxLayout, QWidget

CARD_RADIUS = 8
CARD_INSET = 8
PANEL_GAP = 6
RAIL_SIZE = 40
HEADER_HEIGHT = 36
CONTROL_RADIUS = 6


@dataclass(frozen=True)
class UiColors:
    """一次主题同步使用的不可变界面色板，不回写文档 Theme。"""

    shell: str
    surface: str
    field: str
    text: str
    muted: str
    border: str
    hover: str
    selected: str
    accent: str
    error: str = "#d85858"


def ui_colors(theme: Theme | UiColors) -> UiColors:
    """依据背景明度选择中性外壳，并沿用文档文字色与强调色。

    不按主题名称判断，保证第三方主题、重命名主题及打开旧文件也能正确获得明暗
    外观。这里只读取颜色；画布与导出仍直接使用原来的 theme.background。
    """
    if isinstance(theme, UiColors):
        return theme
    background = QColor(theme.background)
    dark = background.redF() * 0.299 + background.greenF() * 0.587 + background.blueF() * 0.114 < 0.5
    surface = QColor("#2b2d30" if dark else "#ffffff")
    accent = QColor(theme.accent)
    selected = QColor(
        round(surface.red() * 0.84 + accent.red() * 0.16),
        round(surface.green() * 0.84 + accent.green() * 0.16),
        round(surface.blue() * 0.84 + accent.blue() * 0.16),
    ).name()
    return UiColors(
        shell="#1e1f22" if dark else "#e9eaee",
        surface=surface.name(),
        field="#303237" if dark else "#ffffff",
        text=theme.text,
        muted="#92959e" if dark else "#80848d",
        border="#4b4d52" if dark else "#d5d7de",
        hover="#393b40" if dark else "#f1f2f5",
        selected=selected,
        accent=theme.accent,
    )


def sidebar_surface(theme: Theme) -> str:
    """返回所有侧栏共用的底色，停靠外壳、滚动区和内容页必须共同引用。

    明暗判断沿用界面色板，兼容改名及自定义主题。仅派生侧栏背景，不修改通用
    surface 或 field，因此独立属性弹窗、菜单和输入框仍保留原有的视觉层次。
    """
    base = ui_colors(theme)
    if isinstance(theme, UiColors):
        return theme.surface
    return base.surface if QColor(base.surface).lightnessF() < 0.5 else "#f5f6f8"


def outline_colors(theme: Theme) -> UiColors:
    """返回大纲整张卡片共用的色板，供停靠外壳和树视口同步使用。

    浅色使用接近默认画布的浅灰，深色沿用工作区面板底色；原生视口的 Base
    也使用同一颜色，避免滚动、缩放或浮动后在标题和底部圆角留白处露出白条。
    文字、悬停及柔和选中态沿用工作区主题，不修改导图或宿主应用的调色板。
    """
    base = ui_colors(theme)
    surface = sidebar_surface(theme)
    return replace(base, surface=surface, field=surface)


def apply_ui_palette(widget: QWidget, colors: UiColors, *, window: str | None = None) -> None:
    """同步原生子控件与弹层使用的调色板，避免深色菜单、箭头或占位文字失去对比。

    克隆当前控件的调色板，不改动应用或宿主；保留未明确覆盖的系统颜色角色。
    window 可指定外围背景，其余表单、菜单和按钮默认使用卡片底色。
    """
    palette = widget.palette()
    for role, color in (
        (QPalette.ColorRole.Window, window or colors.surface),
        (QPalette.ColorRole.WindowText, colors.text),
        (QPalette.ColorRole.Base, colors.field),
        (QPalette.ColorRole.AlternateBase, colors.hover),
        (QPalette.ColorRole.Text, colors.text),
        (QPalette.ColorRole.PlaceholderText, colors.muted),
        (QPalette.ColorRole.Button, colors.field),
        (QPalette.ColorRole.ButtonText, colors.text),
        (QPalette.ColorRole.Highlight, colors.selected),
        (QPalette.ColorRole.HighlightedText, colors.text),
        (QPalette.ColorRole.ToolTipBase, colors.surface),
        (QPalette.ColorRole.ToolTipText, colors.text),
        (QPalette.ColorRole.Link, colors.accent),
    ):
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors.muted))
    widget.setPalette(palette)


def menu_styles(colors: UiColors) -> str:
    """共用菜单样式，覆盖右键、溢出、位置菜单；不改变动作或键盘导航逻辑。"""
    return (
        f"QMenu {{ background: {colors.surface}; color: {colors.text}; border: 1px solid {colors.border}; "
        f"border-radius: {CONTROL_RADIUS}px; padding: 4px; }}"
        "QMenu::item { padding: 6px 24px; border-radius: 4px; }"
        f"QMenu::item:selected {{ background: {colors.selected}; color: {colors.text}; }}"
        f"QMenu::item:disabled {{ color: {colors.muted}; }}"
        f"QMenu::separator {{ background: {colors.border}; height: 1px; margin: 4px 8px; }}"
        f"QToolTip {{ background: {colors.surface}; color: {colors.text}; "
        f"border: 1px solid {colors.border}; padding: 4px 6px; }}"
    )


def control_styles(colors: UiColors) -> str:
    """表单和应用自有弹窗共用的控件样式；保持原生输入、混合值及校验行为。"""
    return (
        f"QLabel, QCheckBox, QRadioButton {{ color: {colors.text}; background: transparent; }}"
        "QCheckBox, QRadioButton { spacing: 6px; }"
        f"QLineEdit, QDoubleSpinBox, QComboBox {{ background: {colors.field}; color: {colors.text}; "
        f"border: 1px solid {colors.border}; border-radius: {CONTROL_RADIUS}px; padding: 3px 5px; }}"
        f"QLineEdit:hover, QDoubleSpinBox:hover, QComboBox:hover {{ border-color: {colors.muted}; }}"
        f"QLineEdit:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {colors.accent}; }}"
        "QDoubleSpinBox { padding-right: 20px; }"
        "QDoubleSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; width: 18px; height: 16px; border: none; }"
        "QDoubleSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; width: 18px; height: 16px; border: none; }"
        "QComboBox QLineEdit { border: none; background: transparent; padding: 0; }"
        "QComboBox { padding-right: 24px; }"
        "QComboBox::drop-down { border: none; width: 20px; }"
        "QComboBox::down-arrow { image: none; }"
        f"QComboBox QAbstractItemView {{ background: {colors.surface}; color: {colors.text}; "
        f"selection-background-color: {colors.selected}; selection-color: {colors.text}; "
        f"border: 1px solid {colors.border}; outline: none; }}"
        f"QToolButton {{ color: {colors.text}; background: transparent; border: 1px solid transparent; "
        f"border-radius: {CONTROL_RADIUS}px; padding: 2px; }}"
        "QToolButton::menu-button { border: none; width: 14px; }"
        f"QToolButton:hover {{ background: {colors.hover}; border-color: {colors.border}; }}"
        f"QToolButton:checked, QToolButton:pressed {{ background: {colors.selected}; border-color: {colors.accent}; }}"
        f"QToolButton:focus {{ border-color: {colors.accent}; }}"
        f'QToolButton[mixed="true"] {{ border: 1px dashed {colors.accent}; background: {colors.selected}; }}'
        f'QToolButton[color_swatch="true"] {{ background: {colors.field}; border-color: {colors.border}; padding: 2px 4px; }}'
        f"QPushButton {{ background: {colors.field}; color: {colors.text}; border: 1px solid {colors.border}; "
        f"border-radius: {CONTROL_RADIUS}px; padding: 6px 18px; min-width: 44px; }}"
        f"QPushButton:hover {{ background: {colors.hover}; border-color: {colors.muted}; }}"
        f"QPushButton:default, QPushButton:focus {{ border-color: {colors.accent}; }}"
        f"QPushButton:pressed {{ background: {colors.selected}; }}"
        f"QLineEdit:disabled, QDoubleSpinBox:disabled, QComboBox:disabled, QToolButton:disabled, "
        f"QPushButton:disabled, QCheckBox:disabled, QRadioButton:disabled {{ color: {colors.muted}; }}"
        f"QToolButton:disabled {{ background: transparent; border-color: transparent; }}"
        f'QWidget[invalid="true"] {{ border: 1px solid {colors.error}; }}'
        f"QLabel#property_error {{ color: {colors.error}; }}"
    )


def scrollbar_styles(colors: UiColors) -> str:
    """使用窄滚动条并保留原生滚动范围；箭头和空白轨道不再分割卡片。"""
    return (
        "QScrollBar:vertical { background: transparent; width: 10px; margin: 2px 0; }"
        "QScrollBar:horizontal { background: transparent; height: 10px; margin: 0 2px; }"
        f"QScrollBar::handle {{ background: {colors.border}; border-radius: 4px; }}"
        "QScrollBar::handle:vertical { min-height: 24px; margin: 0 2px; }"
        "QScrollBar::handle:horizontal { min-width: 24px; margin: 2px 0; }"
        f"QScrollBar::handle:hover {{ background: {colors.muted}; }}"
        "QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }"
        "QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }"
    )


def dialog_styles(colors: UiColors) -> str:
    """仅给应用自有 Qt 弹窗着色；不请求替换原生文件、颜色选择器或系统标题栏。"""
    return (
        f"QDialog, QMessageBox, QInputDialog {{ background: {colors.surface}; color: {colors.text}; }}"
        + control_styles(colors)
        + menu_styles(colors)
    )


def toolbar_styles(colors: UiColors) -> str:
    """顶部和常驻按钮栏共用背景；图标菜单保留原有紧凑命中尺寸。"""
    return (
        f"QToolBar {{ background: {colors.shell}; color: {colors.text}; "
        "border: none; spacing: 3px; padding: 3px; }"
        f"QToolBar::separator {{ background: {colors.border}; width: 1px; margin: 8px 5px; }}"
        f"QToolButton {{ color: {colors.text}; background: transparent; padding: 5px; "
        f"border: 1px solid transparent; border-radius: {CONTROL_RADIUS}px; }}"
        'QToolButton[mindmap_layout_menu="true"], QToolButton[mindmap_popup_menu="true"] { padding-right: 16px; }'
        'QToolButton[mindmap_layout_menu="true"]::menu-indicator, QToolButton[mindmap_popup_menu="true"]::menu-indicator { '
        "subcontrol-origin: padding; subcontrol-position: right center; right: 5px; width: 8px; height: 8px; }"
        f"QToolButton:hover {{ background: {colors.hover}; border-color: transparent; }}"
        f"QToolButton:pressed, QToolButton:checked {{ background: {colors.selected}; border-color: transparent; }}"
        f"QToolButton:focus {{ border: 1px solid {colors.accent}; }}"
        f"QToolButton:disabled {{ color: {colors.muted}; background: transparent; border-color: transparent; }}"
        'QToolButton[mindmap_icon_option="true"] { padding: 0; border: none; border-radius: 5px; }'
        f'QToolButton[mindmap_icon_option="true"]:focus {{ background: {colors.selected}; }}'
        + menu_styles(colors)
        + 'QMenu[mindmap_icon_menu="true"] { border: none; padding: 6px; }'
    )


def workspace_styles(colors: UiColors, *, sidebar: str | None = None) -> str:
    """原生停靠分隔条作为卡片间隙，标题背景透明以露出同一块圆角表面。

    sidebar 仅控制工具窗口外壳，避免将侧栏底色传入菜单样式；浮动窗口仍属于
    工作区的控件树，同一规则也覆盖其标题和底部圆角留白。
    """
    return (
        f"QMainWindow#mindmap_workspace {{ background: {colors.shell}; }}"
        f'QDockWidget[mindmap_tool_dock="true"] {{ background: {sidebar or colors.surface}; '
        f"border: none; border-radius: {CARD_RADIUS}px; }}"
        "QWidget#tool_window_header, QWidget#tool_window_body { background: transparent; }"
        f"QLabel#tool_window_title {{ color: {colors.text}; }}"
        f"QWidget#tool_window_header QToolButton {{ color: {colors.muted}; border: 1px solid transparent; "
        f"padding: 3px; border-radius: {CONTROL_RADIUS}px; }}"
        f"QWidget#tool_window_header QToolButton:hover {{ background: {colors.hover}; color: {colors.text}; }}"
        f"QWidget#tool_window_header QToolButton:focus {{ border-color: {colors.accent}; }}"
        f"QMainWindow#mindmap_workspace::separator {{ background: {colors.shell}; "
        f"width: {PANEL_GAP}px; height: {PANEL_GAP}px; }}"
        f"QMainWindow#mindmap_workspace::separator:hover {{ background: {colors.selected}; }}"
        + menu_styles(colors)
    )


class ComboBox(QComboBox):
    """仅补充下拉箭头的组合框，原生弹出、输入、选择及无障碍语义保持不变。

    Windows 的原生箭头在 QSS 自定义下拉区域后可能消失或沿用黑色；使用实际子控件
    区域和当前调色板绘制矢量箭头，保证明暗、高 DPI 及从右到左排列均正确。
    """

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxArrow, self
        )
        center = rect.center()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        painter.setPen(
            QPen(
                self.palette().color(group, QPalette.ColorRole.Text),
                1.5,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(center.x() - 3, center.y() - 1.5),
                    QPointF(center.x(), center.y() + 1.5),
                    QPointF(center.x() + 3, center.y() - 1.5),
                ]
            )
        )


class CardFrame(QWidget):
    """画布承载层；独立使用时留出圆角半径，嵌入编辑器时由宿主管理外轮廓。

    不对 QGraphicsScene 使用遮罩、特效或样式表。卡片仅在普通 QWidget 中绘制，
    因而不改变节点测量、图形边界和导出；内边距由 Qt 统一处理高 DPI 缩放。
    """

    def __init__(self, content: QWidget, parent=None, *, rounded=True):
        """rounded=False 时使用直角和零内边距，让视口与宿主标签栏直接衔接。"""
        super().__init__(parent)
        self.setObjectName("mindmap_canvas_card")
        self.background = "#ffffff"
        self.rounded = rounded
        layout = QVBoxLayout(self)
        inset = CARD_INSET if rounded else 0
        layout.setContentsMargins(inset, inset, inset, inset)
        layout.setSpacing(0)
        layout.addWidget(content)

    def set_background(self, color: str) -> None:
        """仅更新承载层的背景；不触碰视口状态、输入焦点或文档。"""
        if self.background != color:
            self.background = color
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.background))
        radius = CARD_RADIUS if self.rounded else 0
        painter.drawRoundedRect(QRectF(self.rect()), radius, radius)
