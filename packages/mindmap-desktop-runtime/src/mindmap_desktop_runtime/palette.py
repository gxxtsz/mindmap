"""平台自身的中性色板，不依赖任何编辑器的主题或图标实现。"""

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette

from .ui.docking import Theme


@dataclass(frozen=True)
class UiColors:
    shell: str = "#f4f4f4"
    surface: str = "#ffffff"
    field: str = "#ffffff"
    text: str = "#262626"
    muted: str = "#777777"
    border: str = "#dedede"
    hover: str = "#eeeeee"
    selected: str = "#dbeafe"
    accent: str = "#4583c5"
    error: str = "#d64545"


def colors_from_theme(theme):
    keys = ("main-window-bg", "tool-window-bg", "editor-bg", "text-default", "text-muted",
            "tool-window-border", "toolbar-bg-hovered", "toolbar-selected-bg-active",
            "control-brand-border", "danger")
    return UiColors(*(theme.color(key).name() for key in keys))


def apply_ui_palette(widget, colors):
    palette = widget.palette()
    for role, color in ((QPalette.ColorRole.Window, colors.surface),
                        (QPalette.ColorRole.Base, colors.field), (QPalette.ColorRole.Text, colors.text),
                        (QPalette.ColorRole.WindowText, colors.text),
                        (QPalette.ColorRole.Highlight, colors.selected),
                        (QPalette.ColorRole.HighlightedText, colors.text)):
        palette.setColor(role, QColor(color))
    widget.setPalette(palette)


def menu_styles(colors):
    return f"QMenu {{background:{colors.surface}; color:{colors.text};}}"


def scrollbar_styles(colors):
    return f"QScrollBar {{background:{colors.surface};}}"


def toolbar_icon(name, *colors):
    theme = Theme()
    return theme.icons.get_icon(name, color=colors[0], size=20) if colors else theme.icon(name)
