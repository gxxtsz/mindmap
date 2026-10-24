"""工具栏共用的原创 SVG 图标与主题样式；不依赖外部图片或图标字体。"""

from __future__ import annotations

from functools import lru_cache

from mindmap_core import Theme
from mindmap_qt_render import page_icon_body
from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QMenu, QToolBar, QToolButton
from shiboken6 import isValid

from .ui_style import apply_ui_palette, toolbar_styles, ui_colors

_ICON_SIZE = 20
_MENU_OPTION_SIZE = 32
_MENU_PADDING = 6
_ICON_PROPERTY = "mindmaptoolbar_icon"
_THEME_PROPERTY = "mindmap_toolbar_theme"


class IconOptionMenu(QMenu):
    """布局与滚轮共用的窄条菜单，保留 QMenu 的键盘导航和自动关闭行为。

    菜单宽度由 32 像素选项及两侧各 6 像素留白组成，与工具栏的下拉按钮等宽。
    所有尺寸均为 Qt 逻辑像素，高 DPI 下由 Qt 自动缩放；外观随工具栏主题更新。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("mindmap_icon_menu", True)
        self.setFixedWidth(_MENU_OPTION_SIZE + 2 * _MENU_PADDING)
        self.setToolTipsVisible(True)


class IconMenuOptionButton(QToolButton):
    """菜单中的纯图标选项，复用动作状态，通过背景高亮显示当前项。

    布局方向与滚轮行为共用相同尺寸和绘制规则；动作的中文名称只供提示、键盘
    菜单和无障碍读取。保留动作的互斥选中语义，由 Qt 和主题样式绘制图标及高亮。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        # 方形固定尺寸避免动作的中文名称或原菜单的最小宽度重新撑大弹层。
        self.setFixedSize(_MENU_OPTION_SIZE, _MENU_OPTION_SIZE)
        self.setProperty("mindmap_icon_option", True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)


# 所有图形共用 24×24 坐标、两单位描边和圆角端点；只保留界面实际引用的图形。
_SVG_BODIES = {
    # 页面类型与节点、矢量导出复用相同图形体，主题和高 DPI 状态仍由本工厂管理。
    "main": page_icon_body("main"),
    "detail": page_icon_body("detail"),
    "submap": page_icon_body("submap"),
    "page_up": '<path d="M12 21V4m-6 6 6-6 6 6"/>',
    "favorite": '<path d="m12 3 2.8 5.7 6.3.9-4.6 4.5 1.1 6.3-5.6-3-5.6 3 1.1-6.3L2.9 9.6l6.3-.9Z"/>',
    "favorite_on": (
        '<path fill="currentColor" '
        'd="m12 3 2.8 5.7 6.3.9-4.6 4.5 1.1 6.3-5.6-3-5.6 3 1.1-6.3L2.9 9.6l6.3-.9Z"/>'
    ),
    "panel_menu": '<circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/>',
    "panel_hide": '<path d="M5 12h14"/>',
    # 日志标题栏复用同一组线条、禁用及高 DPI 状态；图形分别表示复制全部和清空。
    "copy": '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
    "clear": '<path d="M4 6h16M9 6V4h6v2M6 6l1 14h10l1-14M10 10v6m4-6v6"/>',
    # 逐级缩进的横线配向下箭头表示展开层级，避免用数字和大下拉箭头挤占标题栏。
    "outline_depth": '<path d="M4 4h16M9 12h11M13 20h7M4 9v11m-2-2 2 2 2-2"/>',
    # 属性按钮使用原创线条图形，避免依赖某台电脑的图标字体或外部位图资源。
    "font_bold": '<path d="M7 4h6a4 4 0 0 1 0 8H7m6 0a4 4 0 0 1 0 8H7V4"/>',
    "font_italic": '<path d="M10 4h9M5 20h9M15 4 9 20"/>',
    "font_underline": '<path d="M6 4v7a6 6 0 0 0 12 0V4M4 21h16"/>',
    "font_increase": '<path d="m3 19 5-14 5 14M5 14h6m7-10v8m-4-4h8"/>',
    "font_decrease": '<path d="m3 19 5-14 5 14M5 14h6m3-6h8"/>',
    "outline": (
        '<path d="M5 4v16m0-13h5m-5 6h5m-5 6h5"/>'
        '<rect x="11" y="4" width="9" height="5" rx="1"/>'
        '<path d="M12 13h8m-8 6h8"/>'
    ),
    "properties": (
        '<path d="M4 6h3m4 0h9M4 12h9m4 0h3M4 18h3m4 0h9"/>'
        '<circle cx="9" cy="6" r="2"/><circle cx="15" cy="12" r="2"/>'
        '<circle cx="9" cy="18" r="2"/>'
    ),
    "settings": (
        '<path d="m10 3-.5 2-2 1-2-.6-2 3.4L5 10.3v2.4l-1.5 1.5 2 3.4 2-.6 2 1 .5 2h4l.5-2 '
        '2-1 2 .6 2-3.4-1.5-1.5v-2.4l1.5-1.5-2-3.4-2 .6-2-1-.5-2Z"/>'
        '<circle cx="12" cy="11.5" r="3"/>'
    ),
    # 调色板表示文档中的自定义配色，避免与打开设置面板的齿轮混淆。
    "palette": (
        '<path d="M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 1.4-3.4 1.5 1.5 0 0 1 1.1-2.6H18'
        'a3 3 0 0 0 3-3 9 9 0 0 0-9-9Z"/>'
        '<circle cx="7" cy="11" r=".8"/><circle cx="10" cy="7" r=".8"/>'
        '<circle cx="15" cy="7" r=".8"/><circle cx="17" cy="11" r=".8"/>'
    ),
    "undo": '<path d="m9 5-5 5 5 5M4 10h9a6 6 0 0 1 6 6v3"/>',
    "inspiration": ('<path d="M9 17h6m-5 3h4M8.5 14.5a6 6 0 1 1 7 0L15 17H9Z"/>'
                    '<path d="M12 1v1M3 5l1 1m16-1-1 1M1 12h2m18 0h2"/>'),
    "redo": '<path d="m15 5 5 5-5 5M20 10h-9a6 6 0 0 0-6 6v3"/>',
    "fit": ('<path d="M8 3H3v5m13-5h5v5M3 16v5h5m8 0h5v-5"/><rect x="8" y="8" width="8" height="8" rx="1"/>'),
    # 环形逆时针箭头表示恢复原始缩放；与其他按钮共用线宽和圆角，适配小尺寸显示。
    "reset_zoom": '<path d="M3 12a9 9 0 1 0 9-9c-3.5 0-6.5 1.8-8 4.5L3 8m0-5v5h5"/>',
    "center": (
        '<circle cx="12" cy="12" r="6"/>'
        '<path d="M12 3v3m0 12v3M3 12h3m12 0h3"/>'
        '<circle cx="12" cy="12" r="1"/>'
    ),
    "wheel_scroll": (
        '<rect x="7" y="3" width="10" height="18" rx="5"/>'
        '<path d="M12 6v4M3 7V3m-2 2 2-2 2 2M21 17v4m-2-2 2 2 2-2"/>'
    ),
    "wheel_zoom": (
        '<rect x="2" y="3" width="9" height="16" rx="4.5"/>'
        '<path d="M6.5 6v3"/><circle cx="16" cy="12" r="4"/>'
        '<path d="m19 15 3 4M16 10v4m-2-2h4"/>'
    ),
    "layout_both": (
        '<rect x="9" y="9" width="6" height="6" rx="1"/>'
        '<rect x="2" y="3" width="4" height="4" rx="1"/>'
        '<rect x="2" y="17" width="4" height="4" rx="1"/>'
        '<rect x="18" y="3" width="4" height="4" rx="1"/>'
        '<rect x="18" y="17" width="4" height="4" rx="1"/>'
        '<path d="M9 12H4V7m0 5v5m11-5h5V7m0 5v5"/>'
    ),
    "layout_right": (
        '<rect x="3" y="9" width="6" height="6" rx="1"/>'
        '<rect x="15" y="3" width="6" height="6" rx="1"/>'
        '<rect x="15" y="15" width="6" height="6" rx="1"/>'
        '<path d="M9 12h3V6h3m-3 6v6h3"/>'
    ),
    "layout_left": (
        '<rect x="15" y="9" width="6" height="6" rx="1"/>'
        '<rect x="3" y="3" width="6" height="6" rx="1"/>'
        '<rect x="3" y="15" width="6" height="6" rx="1"/>'
        '<path d="M15 12h-3V6H9m3 6v6H9"/>'
    ),
    "layout_down": (
        '<rect x="9" y="3" width="6" height="6" rx="1"/>'
        '<rect x="3" y="15" width="6" height="6" rx="1"/>'
        '<rect x="15" y="15" width="6" height="6" rx="1"/>'
        '<path d="M12 9v3H6v3m6-3h6v3"/>'
    ),
    "moon": '<path d="M20.5 13.2A8.5 8.5 0 0 1 10.8 3.5a8.5 8.5 0 1 0 9.7 9.7Z"/>',
    "sun": (
        '<circle cx="12" cy="12" r="4"/>'
        '<path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4'
        'M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'
    ),
}


@lru_cache(maxsize=128)
def toolbar_icon(name: str, foreground: str, disabled: str, accent: str) -> QIcon:
    """按图形和主题颜色缓存图标；仅在已有 QApplication 的 GUI 线程调用。

    SVG 按多档物理分辨率直接绘制，避免先生成小位图再放大；显式提供禁用和选中
    图像，使只读状态及紧凑布局开关在明暗主题中都保持清晰、稳定的视觉反馈。
    name 必须为内置图标名称，未知名称抛出 ValueError。三个颜色分别用于普通、
    禁用和选中状态，返回的 QIcon 不持有文档或引擎，调用方可赋给自己的动作。
    """
    if name not in _SVG_BODIES:
        raise ValueError(f"工具栏图标不存在：{name}")
    icon = QIcon()
    variants = (
        (foreground, ((QIcon.Mode.Normal, QIcon.State.Off),)),
        (disabled, ((QIcon.Mode.Disabled, QIcon.State.Off), (QIcon.Mode.Disabled, QIcon.State.On))),
        (
            accent,
            (
                (QIcon.Mode.Active, QIcon.State.Off),
                (QIcon.Mode.Normal, QIcon.State.On),
                (QIcon.Mode.Active, QIcon.State.On),
            ),
        ),
    )
    for color, states in variants:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
            f'color="{QColor(color).name()}" fill="none" stroke="{QColor(color).name()}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round">{_SVG_BODIES[name]}</svg>'
        )
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        for ratio in (1.0, 1.5, 2.0, 3.0):
            pixels = round(_ICON_SIZE * ratio)
            pixmap = QPixmap(pixels, pixels)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()
            # 绘制完毕再设置像素比：渲染时使用物理坐标，交给 Qt 显示时保持 20 个逻辑像素。
            pixmap.setDevicePixelRatio(ratio)
            for mode, state in states:
                icon.addPixmap(pixmap, mode, state)
    return icon


def add_icon_action(toolbar: QToolBar, action: QAction, icon_name: str) -> None:
    """将已有动作以纯图标按钮加入工具栏，保留其中文名称、快捷键和信号连接。

    toolbar、action 为 GUI 线程中有效的工具栏和既有动作，icon_name 指定内置图标名称；
    未知图标抛出 ValueError，成功返回 None。本函数不转移动作所有权、不创建替代动作，
    菜单仍可复用同一 QAction。名称或快捷键变化时同步当前按钮的提示及无障碍信息；
    按钮移除或工具栏销毁后停止更新，不持有已销毁的按钮，允许动作继续被其他界面使用。
    """
    if icon_name not in _SVG_BODIES:
        raise ValueError(f"工具栏图标不存在：{icon_name}")
    toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    toolbar.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
    action.setProperty(_ICON_PROPERTY, icon_name)
    toolbar.addAction(action)
    button = toolbar.widgetForAction(action)
    if isinstance(button, QToolButton):
        button.setMinimumSize(32, 32)
        # 允许键盘聚焦，但鼠标点击视图按钮时不抢走画布焦点，方向键仍可用于节点导航。
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)

        def sync_description():
            """直接更新按钮说明，避免改写 QAction 再次触发 changed 信号。"""
            if not isValid(toolbar) or not isValid(action):
                return
            # 分屏和会话关闭可能先销毁工具栏按钮，原动作仍会继续发送 changed。
            # 每次查询当前承载按钮，避免闭包长期借用已经被 Qt 删除的旧按钮。
            button = toolbar.widgetForAction(action)
            if not isinstance(button, QToolButton) or not isValid(button):
                return
            shortcuts = " / ".join(
                key.toString(QKeySequence.SequenceFormat.NativeText) for key in action.shortcuts()
            )
            label = action.toolTip()
            description = f"{label}（{shortcuts}）" if shortcuts else label
            button.setToolTip(description)
            button.setAccessibleName(label)
            button.setAccessibleDescription(description)

        action.changed.connect(sync_description)
        sync_description()
    # 宿主可能在组件首次刷新后追加动作；使下一次主题同步覆盖这些新按钮。
    toolbar.setProperty(_THEME_PROPERTY, None)


def set_action_icon(action: QAction, icon_name: str, theme: Theme) -> None:
    """按当前状态更新动作图标，并记住名称供后续主题刷新复用。

    布局菜单和主题开关会在颜色不变时更换图形，因此独立于工具栏的主题缓存更新。
    相同图标不重复发送 QAction.changed，避免普通选择变化引发不必要的按钮重绘。
    """
    if icon_name not in _SVG_BODIES:
        raise ValueError(f"工具栏图标不存在：{icon_name}")
    action.setProperty(_ICON_PROPERTY, icon_name)
    colors = ui_colors(theme)
    icon = toolbar_icon(icon_name, colors.text, colors.muted, colors.accent)
    if action.icon().cacheKey() != icon.cacheKey():
        action.setIcon(icon)


def apply_toolbar_theme(toolbar: QToolBar, theme: Theme) -> None:
    """同步工具栏背景、布局菜单和图标；主题不变时跳过重绘资源与样式的重复设置。

    调用方在文档刷新后传入当前主题，因此切换主题、撤销重做和打开文件均走同一条
    更新路径。图标缓存只依赖颜色，普通节点编辑和选中变化不会重复解析 SVG。
    """
    colors = ui_colors(theme)
    signature = colors
    if toolbar.property(_THEME_PROPERTY) == signature:
        return
    toolbar.setStyleSheet(toolbar_styles(colors))
    apply_ui_palette(toolbar, colors, window=colors.shell)
    for action in toolbar.actions():
        name = action.property(_ICON_PROPERTY)
        if name:
            set_action_icon(action, name, theme)
    toolbar.setProperty(_THEME_PROPERTY, signature)
