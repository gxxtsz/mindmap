"""导图编辑控件的视觉参数；界面配色与导图场景、文件主题严格分开。

这里的中性色参照界面截图独立设定，不是从 PyCharm 提取的资源。所有尺寸使用
Qt 逻辑像素，所有样式只施加到调用方拥有的控件，不修改 QApplication 的全局样式。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from mindmap_core import Theme
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget

CARD_RADIUS = 8
CARD_INSET = 8
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


def _is_dark_background(color: QColor) -> bool:
    """用 RGB 加权亮度判断界面底色的明暗，供派生色板共用。"""
    return color.redF() * 0.299 + color.greenF() * 0.587 + color.blueF() * 0.114 < 0.5


def graph_edge_colors(background: QColor) -> tuple[str, str]:
    """按实际画布底色返回普通连线和直接选中连线的颜色，依次为灰蓝色和琥珀色。

    嵌入画布使用宿主提供的底色，独立画布使用文档背景。两种明暗下的色值供图页
    的场景和前景共用；节点关联线仍使用界面强调色，文档主题和导出颜色由调用方保留。
    """
    if _is_dark_background(background):
        return "#98a8bd", "#ffca70"
    return "#65768d", "#ac5208"


def ui_colors(theme: Theme | UiColors) -> UiColors:
    """依据背景明度选择中性外壳，并沿用文档文字色与强调色。

    不按主题名称判断，保证第三方主题、重命名主题及打开旧文件也能正确获得明暗
    外观。这里只读取颜色；画布与导出仍直接使用原来的 theme.background。
    """
    if isinstance(theme, UiColors):
        return theme
    dark = _is_dark_background(QColor(theme.background))
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
