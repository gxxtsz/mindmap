"""共用导图设置：菜单与兼容表单共享提交、校验及状态同步逻辑。"""

from __future__ import annotations

from dataclasses import replace

from mindmap_core import MindMapError, Theme, apply_branch_style
from mindmap_qt_ui.branch_styles import populate_branch_combo
from mindmap_qt_ui.icons import toolbar_icon
from mindmap_qt_ui.property_controls import MIXED, BranchPicker
from mindmap_qt_ui.settings_menu import SettingsMenu
from mindmap_qt_ui.ui_style import (
    ComboBox,
    UiColors,
    apply_ui_palette,
    control_styles,
    menu_styles,
    scrollbar_styles,
    sidebar_surface,
    ui_colors,
)
from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QLabel,
    QMenu,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .contracts import PanelHost


class GlobalSettingsPanel(QWidget):
    """绑定一个画布的整图设置，不访问 QSettings，也不维护第二份文档配置。

    滚轮调用画布偏好接口，宿主继续监听 wheel_mode_changed 保存本机设置；其他
    选项先验证候选文档，再调用公开引擎操作，遵守历史、前置钩子和原子回滚规则。
    """

    def __init__(self, canvas: PanelHost, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self._loading = False
        self._disposed = False
        self._theme_signature = None
        self._ui_colors_override = None
        self._menus = {}
        self.sections = {}
        self.setObjectName("global_settings_panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.page = QWidget()
        self.page.setObjectName("global_settings_page")
        self.page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.body = QVBoxLayout(self.page)
        self.body.setContentsMargins(12, 4, 12, 16)
        self.body.setSpacing(12)
        self.scroll.setWidget(self.page)
        outer.addWidget(self.scroll)

        self.wheel = self._combo("滚轮行为", (("上下滚动", "scroll"), ("缩放", "zoom")))
        self.wheel.setToolTip("普通滚轮的行为；Ctrl＋滚轮始终缩放")
        self._section("滚轮").addRow("滚轮行为", self.wheel)
        layout = self._section("布局")
        self.direction = self._combo(
            "布局方向", (("双向", "both"), ("向右", "right"), ("向左", "left"), ("向下", "down"))
        )
        layout.addRow("布局方向", self.direction)
        self.compact = QCheckBox("启用紧凑布局")
        self.compact.setAccessibleName("紧凑布局")
        layout.addRow(self.compact)
        self.branch = BranchPicker()
        self.branch.setObjectName("global_branch_style")
        self.branch.setAccessibleName("全局分支样式")
        branches = self._section("分支")
        branches.addRow("连接线样式", self.branch)
        hint = QLabel("应用于整张导图，覆盖已有局部分支样式。")
        hint.setWordWrap(True)
        hint.setProperty("settings_hint", True)
        branches.addRow(hint)
        self.theme = self._combo("主题", (("浅色", "light"), ("深色", "dark"), ("自定义", "custom")))
        # 自定义主题只用于反映已有文档；用户必须明确选择内置主题才能替换它。
        self.theme.model().item(self.theme.findData("custom")).setEnabled(False)
        self._section("外观").addRow("主题", self.theme)
        self.body.addStretch(1)

        for key in ("wheel", "direction", "branch", "theme"):
            field = getattr(self, key)
            field.activated.connect(
                lambda index, name=key, combo=field: self._change(name, combo.itemData(index))
            )
        # clicked 只响应用户输入，快照同步 setChecked 不会反向提交文档。
        self.compact.clicked.connect(lambda checked: self._change("compact", checked))
        canvas.document_changing.connect(self._close_popups)
        canvas.document_changed.connect(self.sync)
        canvas.wheel_mode_changed.connect(self.sync)
        canvas.editable_changed.connect(self.sync)
        canvas.editing_changed.connect(self.sync)
        canvas.editable_changed.connect(self._close_popups)
        canvas.editing_changed.connect(self._close_popups)
        self.sync()

    def create_menu(self, parent=None) -> QMenu:
        """为宿主创建独立的“导图设置”菜单，不创建停靠窗口或第二份配置。

        菜单由传入的 QWidget（缺省为本对象）拥有；宿主可将其加入齿轮菜单，
        或调用 popup() 展开。保留原表单接口供已有集成访问，所有菜单动作仍走
        同一提交边界。每次打开绑定当前文档代次，关闭或替换后拒绝迟到事件。
        """
        menu = SettingsMenu("导图设置", parent or self)
        menu.setObjectName("mindmap_settings_menu")
        menu.setToolTipsVisible(True)
        record = {"actions": {}, "active": False, "epoch": 0, "generation": None}
        self._menus[menu] = record
        for key, title in (("wheel", "滚轮行为"), ("direction", "布局方向"),
                           ("compact", "紧凑布局"), ("branch", "连接线样式"), ("theme", "导图主题")):
            options = []
            record["actions"][key] = options
            if key == "compact":
                action = menu.addAction(title)
                action.setCheckable(True)
                action.setObjectName("settings_compact")
                options.append(action)
                action.triggered.connect(lambda checked: self._menu_change(menu, "compact", checked))
                continue
            field = getattr(self, key)
            sub = SettingsMenu(title, menu)
            sub.setProperty("settings_label", title)
            menu.addMenu(sub)
            sub.setObjectName("settings_" + key)
            sub.setToolTipsVisible(True)
            group = QActionGroup(sub)
            group.setExclusive(True)
            # 混合状态只用于展示，不是可提交的连接线类型。
            if key == "branch":
                mixed = sub.addAction("多种样式")
                mixed.setData("__mixed__")
                options.append(mixed)
            for index in range(field.count()):
                value = field.itemData(index)
                if value == "__mixed__":
                    continue
                action = sub.addAction(field.itemIcon(index), field.itemText(index))
                action.setData(value)
                action.setCheckable(True)
                group.addAction(action)
                options.append(action)
                action.triggered.connect(
                    lambda checked=False, name=key, selected=value: self._menu_change(menu, name, selected)
                )
        menu.aboutToShow.connect(lambda: self._open_menu(menu))
        menu.aboutToHide.connect(lambda: self._defer_menu_close(menu))
        menu.destroyed.connect(lambda: self._menus.pop(menu, None))
        self._sync_menu(menu, record)
        return menu

    def _open_menu(self, menu):
        """打开时重新读取正式状态；查看菜单不提交文字编辑或改变属性面板可见性。"""
        if self._disposed:
            return
        record = self._menus[menu]
        record.update(active=True, epoch=record["epoch"] + 1,
                      generation=self.canvas.engine.document_generation)
        self.sync()

    def _defer_menu_close(self, menu):
        """Qt 先隐藏菜单再发出 triggered，延至本轮事件结束才拒绝后续的迟到动作。"""
        record = self._menus[menu]
        epoch = record["epoch"]

        def finish():
            current = self._menus.get(menu)
            if current is not None and current["epoch"] == epoch:
                current["active"] = False

        QTimer.singleShot(0, self, finish)

    def _menu_change(self, menu, key, value):
        """菜单提交复用原表单校验；显式失效、已销毁或来自旧文档的动作均忽略。"""
        record = self._menus.get(menu)
        if (record is None or not record["active"] or self._disposed
                or record["generation"] != self.canvas.engine.document_generation):
            return
        self._change(key, value)

    def _sync_menu(self, menu, record):
        """只同步动作展示，避免 setChecked 反向提交；禁用原因与表单保持一致。"""
        colors = ui_colors(self._ui_colors_override or self.canvas.document_view.theme)
        menu.set_colors(colors)
        for key, actions in record["actions"].items():
            field = getattr(self, key)
            tip = field.toolTip()
            if key == "branch" and field.isEnabled():
                tip += "；应用于整张导图，覆盖已有局部分支样式。"
            if key == "compact":
                actions[0].setChecked(field.isChecked())
                actions[0].setText("紧凑布局：" + ("已启用" if field.isChecked() else "未启用"))
                actions[0].setEnabled(field.isEnabled())
                actions[0].setToolTip(tip)
                continue
            sub = actions[0].parent()
            # 上级菜单直接展示正式值；混合分支、自定义主题也沿用表单中的中文摘要。
            sub.setTitle(f"{sub.property('settings_label')}：{field.currentText()}")
            sub.menuAction().setEnabled(field.isEnabled())
            sub.menuAction().setToolTip(tip)
            sub.set_colors(colors)
            for action in actions:
                value = action.data()
                index = field.findData(value)
                action.setChecked(value == field.currentData())
                font = sub.font()
                font.setBold(action.isChecked())
                action.setFont(font)
                action.setEnabled(field.isEnabled() and index >= 0 and field.model().item(index).isEnabled())
                action.setToolTip(tip)
                if value == "__mixed__":
                    action.setVisible(field.currentData() == "__mixed__")
                elif index >= 0:
                    action.setIcon(field.itemIcon(index))

    @staticmethod
    def _combo(label, options):
        """使用有中文名称的普通选项，侧栏收起与展开时都能辨认当前配置。"""
        field = ComboBox()
        field.setAccessibleName(label)
        field.setMinimumHeight(34)
        field.setMinimumWidth(120)
        for text, value in options:
            field.addItem(text, value)
        return field

    def _section(self, name):
        """分组共用外层滚动区，以标题和细线分隔，避免嵌套卡片占用侧栏空间。"""
        section = QWidget(self.page)
        box = QVBoxLayout(section)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        if self.sections:
            line = QFrame(section)
            line.setObjectName("settings_separator")
            line.setFixedHeight(1)
            box.addWidget(line)
        title = QLabel(name)
        title.setProperty("settings_heading", True)
        title.setMinimumHeight(32)
        box.addWidget(title)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(10)
        box.addLayout(form)
        self.sections[name] = section
        self.body.addWidget(section)
        return form

    def _change(self, key, value):
        """用户明确选择才提交；失败或无效的迟到事件都从正式状态恢复控件显示。"""
        if self._loading or self._disposed:
            return
        field = getattr(self, key)
        try:
            if field.isEnabled():
                self.canvas.call(lambda: self._commit(key, value))
        finally:
            self.sync()

    def _commit(self, key, value):
        """整图操作始终使用根节点，不读取当前选择、聚焦节点或属性面板的局部范围。"""
        if key == "wheel":
            self.canvas.set_wheel_mode(value)
            return
        engine = self.canvas.engine
        generation = engine.document_generation
        # 设置菜单与属性面板可以同时显示；先提交原节点有效草稿，避免全局修改的
        # document_changing 通知将其丢弃。随后重新读取快照，保留刚提交的节点属性。
        self.canvas.edits_flush_requested.emit()
        if self._disposed or generation != engine.document_generation:
            return
        if not self.canvas.editable or self.canvas.is_editing:
            raise MindMapError("请先结束文字编辑并退出只读模式再修改导图设置")
        document = engine.document
        candidate = engine.document
        info = self.canvas.layout_registry.info(document.layout.algorithm_id)
        if key == "direction":
            if value not in info.directions:
                raise MindMapError("当前布局算法不支持该布局方向")
            candidate.layout = replace(document.layout, direction=value)
        elif key == "compact":
            if not info.supports_compact:
                raise MindMapError("当前布局算法不支持紧凑布局")
            candidate.layout = replace(document.layout, compact=value)
        elif key == "theme":
            if value not in ("light", "dark"):
                raise MindMapError("请选择浅色或深色主题")
            candidate.theme = Theme() if value == "light" else Theme.dark()
        elif key == "branch":
            if not info.supports_branch_styles:
                raise MindMapError("当前布局算法不支持分支连接线样式")
            apply_branch_style(candidate, [candidate.root_id], value, scope="subtree")
        if candidate == document:
            return
        # 先验证整份候选文档，包括折叠和聚焦范围外的节点，再走公开引擎事务。
        # 校验、布局或前置钩子拒绝时不会留下部分修改，也不会产生撤销记录。
        self.canvas.validate_document(candidate)
        labels = {
            "direction": "修改全局布局方向",
            "compact": "修改紧凑布局",
            "theme": "切换主题",
            "branch": "统一全局分支样式",
        }
        with engine.transaction(labels[key]):
            if key in ("direction", "compact"):
                engine.set_layout(candidate.layout)
            elif key == "theme":
                engine.set_theme(candidate.theme)
            else:
                engine.set_branch_style([document.root_id], value, scope="subtree")

    def sync(self, *events):
        """从正式状态同步值、混合状态及布局能力；不创建默认配置或触发用户选择信号。"""
        if self._disposed or (events and getattr(events[0], "kind", None) == "saved"):
            return
        # 仅同步控件，复用提交后的界面快照；修改设置时另行建立候选文档。
        document = self.canvas.document_view
        info = self.canvas.layout_registry.info(document.layout.algorithm_id)
        editable = self.canvas.editable and not self.canvas.is_editing
        self._loading = True
        try:
            self.set_theme(document.theme)
            with QSignalBlocker(self.wheel), QSignalBlocker(self.direction), QSignalBlocker(self.compact):
                self.wheel.setCurrentIndex(self.wheel.findData(self.canvas.wheel_mode))
                self.direction.setCurrentIndex(self.direction.findData(document.layout.direction))
                self.compact.setChecked(document.layout.compact)
            for index in range(self.direction.count()):
                self.direction.model().item(index).setEnabled(
                    self.direction.itemData(index) in info.directions
                )
            kinds = self.canvas.branch_kinds([document.root_id])
            kind = next(iter(kinds)) if len(kinds) == 1 else MIXED
            with QSignalBlocker(self.branch), QSignalBlocker(self.theme):
                self.branch.set_value(kind)
                theme = (
                    "light"
                    if document.theme == Theme()
                    else "dark"
                    if document.theme == Theme.dark()
                    else "custom"
                )
                self.theme.setCurrentIndex(self.theme.findData(theme))
            self.wheel.setEnabled(True)
            for field, supported, label in (
                (self.direction, bool(info.directions), "布局方向"),
                (self.compact, info.supports_compact, "紧凑布局"),
                (self.branch, info.supports_branch_styles, "分支连接线样式"),
                (self.theme, True, "主题"),
            ):
                field.setEnabled(editable and supported)
                field.setToolTip(
                    f"当前布局算法不支持{label}"
                    if not supported
                    else "文字编辑期间不能修改导图设置"
                    if self.canvas.is_editing
                    else "只读模式不能修改导图设置"
                    if not self.canvas.editable
                    else self.branch.currentText()
                    if field is self.branch
                    else label
                )
        finally:
            self._loading = False
        for menu, record in self._menus.items():
            self._sync_menu(menu, record)

    def set_ui_colors(self, colors: UiColors | None) -> None:
        """设置独立界面色板；None 恢复宿主文档主题，不提交任何设置或历史。

        非 UiColors 参数抛出 TypeError；菜单及兼容表单共用这一个公开外观入口。
        """
        if colors is not None and not isinstance(colors, UiColors):
            raise TypeError("界面色板必须为 UiColors 或 None")
        self._ui_colors_override = colors
        self.set_theme(self.canvas.document_view.theme)

    def set_theme(self, theme):
        """只更新本面板及弹层配色，保留文档中的自定义主题及宿主应用的全局样式。"""
        theme = self._ui_colors_override or theme
        colors = ui_colors(theme)
        surface = sidebar_surface(theme)
        signature = (colors, surface)
        if signature == self._theme_signature:
            return
        self._theme_signature = signature
        # 容器与滚动区空白共用侧栏底色；字段和弹层仍读取原色板，保持输入边界
        # 清晰。原地刷新样式，不重新同步字段值或设置滚动条位置。
        self.setStyleSheet(
            f"QWidget#global_settings_panel, QWidget#global_settings_page, QScrollArea, "
            f"QScrollArea > QWidget > QWidget {{ background: {surface}; }}"
            'QLabel[settings_heading="true"] { font-size: 15px; font-weight: bold; }'
            f'QLabel[settings_hint="true"] {{ color: {colors.muted}; }}'
            f"QFrame#settings_separator {{ background: {colors.border}; }}"
            + control_styles(colors)
            + menu_styles(colors)
            + scrollbar_styles(colors)
        )
        apply_ui_palette(self, colors, window=surface)
        for field, icons in (
            (self.wheel, ("wheel_scroll", "wheel_zoom")),
            (self.direction, ("layout_both", "layout_right", "layout_left", "layout_down")),
            (self.theme, ("sun", "moon", "palette")),
        ):
            for index, icon in enumerate(icons):
                field.setItemIcon(index, toolbar_icon(icon, colors.text, colors.muted, colors.accent))
            apply_ui_palette(field.view(), colors)
        with QSignalBlocker(self.branch):
            current = self.branch.value()
            self.branch.set_theme(theme)
            self.branch.clear()
            populate_branch_combo(self.branch, theme)
            self.branch.set_value(current)
        for menu, record in self._menus.items():
            self._sync_menu(menu, record)

    def close_popups(self) -> None:
        """关闭本设置对象拥有的全部菜单，供宿主在标签切换及退出前主动收束。

        关闭会使旧菜单动作失效，不提交设置、不改变文档或历史；可重复调用。
        """
        self._close_popups()

    def _close_popups(self, *_):
        """文档替换和面板隐藏后取消旧弹层，阻止迟到的选项操作写入新的文档。"""
        for menu, record in self._menus.items():
            record["active"] = False
            for sub in menu.findChildren(QMenu):
                sub.close()
            menu.close()
        for field in (self.wheel, self.direction, self.branch, self.theme):
            field.hidePopup()

    def hideEvent(self, event):
        self._close_popups()
        super().hideEvent(event)

    def dispose(self):
        """主动结束顶层弹层；工作区销毁后收到的剩余状态通知不再访问画布。"""
        self._disposed = True
        self._close_popups()
