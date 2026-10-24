"""当前导图的布局、外观侧栏与独立滚轮菜单，共享输入生命周期。"""

from __future__ import annotations

from dataclasses import replace

from mindmap_core import MindMapError, Theme, apply_branch_style
from mindmap_layouts import resolve_tree_spacing
from mindmap_qt_ui.branch_styles import populate_branch_combo
from mindmap_qt_ui.icons import toolbar_icon
from mindmap_qt_ui.layout_controls import LayoutPicker, SpacingSpinBox
from mindmap_qt_ui.property_controls import MIXED, BranchPicker, FlowLayout
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
    QButtonGroup,
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .contracts import PanelHost


class GlobalSettingsPanel(QWidget):
    """绑定一个画布的整图设置，不访问 QSettings，也不维护第二份文档配置。

    滚轮调用画布偏好接口，宿主继续监听 wheel_mode_changed 保存本机设置；其他
    选项先验证候选文档，再调用公开引擎操作，遵守历史、前置钩子和原子回滚规则。
    """

    SPACING_PRESETS = (("紧凑", (32.0, 10.0)), ("适中", (64.0, 22.0)), ("宽松", (96.0, 36.0)))

    def __init__(self, canvas: PanelHost, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self._loading = False
        self._disposed = False
        self._applying = False
        self._pending = {}
        self._generation = canvas.engine.document_generation
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.flush)
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

        # 滚轮属于宿主的本机偏好，紧凑布尔值只保留兼容字段接口；两者均不
        # 占用侧栏。隐藏容器持有兼容控件，避免它们成为无主的顶层窗口。
        self._menu_fields = QWidget(self)
        self._menu_fields.hide()
        self.wheel = self._combo("滚轮行为", (("上下滚动", "scroll"), ("缩放", "zoom")))
        self.wheel.setToolTip("普通滚轮的行为；Ctrl＋滚轮始终缩放")
        layout = self._section("导图布局")
        self.direction = LayoutPicker()
        structure = QWidget()
        structure_row = QHBoxLayout(structure)
        structure_row.setContentsMargins(0, 0, 0, 0)
        structure_row.addWidget(QLabel("布局结构"))
        structure_row.addStretch(1)
        structure_row.addWidget(self.direction)
        layout.addRow(structure)
        layout.addRow(QLabel("主题间距"))
        presets = QWidget()
        preset_layout = FlowLayout(presets)
        self.preset_group = QButtonGroup(self)
        self.preset_buttons = []
        for text, gaps in self.SPACING_PRESETS:
            button = QToolButton(presets)
            button.setText(text)
            button.setProperty("spacing_preset", True)
            button.setCheckable(True)
            button.setMinimumSize(76, 34)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setAccessibleName("主题间距：" + text)
            button.setToolTip(f"父子间距 {gaps[0]:g}，同级间距 {gaps[1]:g}")
            self.preset_group.addButton(button)
            self.preset_buttons.append(button)
            preset_layout.addWidget(button)
            button.clicked.connect(lambda checked=False, values=gaps:
                                   self._change("spacing", dict(zip(("level_gap", "sibling_gap"), values))))
        layout.addRow(presets)
        numbers = QWidget()
        number_layout = FlowLayout(numbers)
        for axis, text in (("horizontal", "水平"), ("vertical", "竖直")):
            cell = QWidget(numbers)
            cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            row = QHBoxLayout(cell)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            row.addWidget(QLabel(text))
            field = SpacingSpinBox(cell)
            field.setAccessibleName(text + "主题间距")
            setattr(self, axis, field)
            row.addWidget(field)
            number_layout.addWidget(cell)
            field.valueChanged.connect(lambda value, name=axis: self._number_changed(name, value))
            field.editingFinished.connect(self.flush)
            field.composition_finished.connect(self._resume_input)
        layout.addRow(numbers)
        self.compact = QCheckBox("启用紧凑布局")
        self.compact.setAccessibleName("紧凑布局")
        appearance = self._section("导图外观")
        self.branch = BranchPicker()
        self.branch.setObjectName("global_branch_style")
        self.branch.setAccessibleName("整图连接线样式")
        self._add_choice_row(appearance, "连接线样式", self.branch)
        self.theme = self._combo("导图主题", (("浅色", "light"), ("深色", "dark"), ("自定义", "custom")))
        self.theme.setFixedWidth(120)
        self._add_choice_row(appearance, "导图主题", self.theme)
        # 自定义主题只用于反映已有文档；用户必须明确选择内置主题才能替换它。
        self.theme.model().item(self.theme.findData("custom")).setEnabled(False)
        for field in (self.wheel, self.compact):
            field.setParent(self._menu_fields)
        self.body.addStretch(1)

        for key in ("wheel", "direction", "branch", "theme"):
            field = getattr(self, key)
            field.activated.connect(
                lambda index, name=key, combo=field: self._change(name, combo.itemData(index))
            )
        # clicked 只响应用户输入，快照同步 setChecked 不会反向提交文档。
        self.compact.clicked.connect(lambda checked: self._change("compact", checked))
        canvas.document_changing.connect(self._document_changing)
        canvas.document_changed.connect(self.sync)
        canvas.wheel_mode_changed.connect(self.sync)
        canvas.editable_changed.connect(self._mode_changed)
        canvas.editing_changed.connect(self._mode_changed)
        self.sync()

    def create_menu(self, parent=None) -> QMenu:
        """创建可直接加入宿主“设置”的滚轮菜单，不包含导图内容设置。

        保留原调用签名和 QWidget 所有权约定：parent 缺省为本对象，宿主可
        直接 addMenu()，也可 popup()。菜单标题展示当前滚轮行为，选项与公开
        wheel 字段共用模型；打开时绑定文档代次，关闭或替换后拒绝迟到动作。
        """
        menu = SettingsMenu("滚轮行为", parent or self)
        menu.setObjectName("settings_wheel")
        menu.setToolTipsVisible(True)
        record = {"active": False, "epoch": 0, "generation": None}
        self._menus[menu] = record
        group = QActionGroup(menu)
        group.setExclusive(True)
        for index in range(self.wheel.count()):
            value = self.wheel.itemData(index)
            action = menu.addAction(self.wheel.itemIcon(index), self.wheel.itemText(index))
            action.setData(value)
            action.setCheckable(True)
            group.addAction(action)
            action.triggered.connect(
                lambda checked=False, selected=value: self._menu_change(menu, selected)
            )
        menu.aboutToShow.connect(lambda: self._open_menu(menu))
        menu.aboutToHide.connect(lambda: self._defer_menu_close(menu))
        menu.destroyed.connect(lambda: self._menus.pop(menu, None))
        self._sync_menu(menu)
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

    def _menu_change(self, menu, value):
        """只提交滚轮偏好；显式失效、已销毁或来自旧文档的动作均忽略。"""
        record = self._menus.get(menu)
        if (record is None or not record["active"] or self._disposed
                or record["generation"] != self.canvas.engine.document_generation):
            return
        self._change("wheel", value)

    def _sync_menu(self, menu):
        """从滚轮模型同步标题、图标和勾选，查看菜单不触发偏好或文档写入。"""
        colors = ui_colors(self._ui_colors_override or self.canvas.document_view.theme)
        menu.set_colors(colors)
        field = self.wheel
        menu.setTitle(f"滚轮行为：{field.currentText()}")
        menu.menuAction().setEnabled(field.isEnabled())
        menu.menuAction().setToolTip(field.toolTip())
        # 不长期缓存 QAction 的 Python 包装对象；临时主菜单释放后，Qt 仍
        # 拥有动作，但 PySide 可能重建包装对象，逐次读取可以安全继续同步。
        for action in menu.actions():
            value = action.data()
            index = field.findData(value)
            action.setChecked(value == field.currentData())
            font = menu.font()
            font.setBold(action.isChecked())
            action.setFont(font)
            action.setEnabled(field.isEnabled() and index >= 0 and field.model().item(index).isEnabled())
            action.setToolTip(field.toolTip())
            if index >= 0:
                action.setIcon(field.itemIcon(index))

    @staticmethod
    def _add_choice_row(form, text, field):
        """标签在左、选择器靠右；窄侧栏由表单自动换行，避免挤压控件内容。"""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        row.addWidget(field)
        label = QLabel(text)
        label.setBuddy(field)
        form.addRow(label, holder)

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
        field = self.horizontal if key == "spacing" else getattr(self, key)
        # 离散选择先提交已完成的数值草稿；本轮提交期间屏蔽递归 flush 和自身通知。
        self.flush()
        self._applying = True
        try:
            if field.isEnabled():
                self.canvas.call(lambda: self._commit(key, value))
        finally:
            self._applying = False
            self.sync()

    @property
    def composing(self) -> bool:
        """汇总数值控件的输入法候选，供宿主保存、关闭时等待用户完成。"""
        return not self._disposed and any(field.composing for field in (self.horizontal, self.vertical))

    def _number_changed(self, axis, value):
        """缓存逻辑方向的间距，防抖期间不建立文档副本或长期占用引擎事务。"""
        field = getattr(self, axis)
        if self._loading or self._disposed or self._applying or not field.isEnabled():
            return
        down = self.canvas.document_view.layout.direction == "down"
        key = "sibling_gap" if (axis == "horizontal") == down else "level_gap"
        self._pending[key] = value
        self._generation = self.canvas.engine.document_generation
        self._sync_presets()
        if not self.composing:
            self.timer.start()

    def _resume_input(self):
        if self._pending and not self.composing:
            self.timer.start()

    def discard_pending(self) -> None:
        """撤销、替换文档或释放组件时取消数值草稿，禁止旧输入延迟写回。"""
        self.timer.stop()
        self._pending.clear()
        loading, self._loading = self._loading, True
        try:
            for field in (self.horizontal, self.vertical):
                field.discard_composition()
        finally:
            self._loading = loading

    def flush(self) -> None:
        """提交当前导图的有效间距，一轮连续输入只形成一次撤销记录。

        保存、失焦及防抖共用此入口；候选未完成、文档已替换或权限改变时不提交。
        _applying 防止 edits_flush_requested 再次进入本对象，同时允许节点属性
        先写回自己的有效输入，随后以最新快照构造布局候选。
        """
        if self._disposed or self._applying or self.composing:
            return
        self.timer.stop()
        if not self._pending:
            return
        if (self._generation != self.canvas.engine.document_generation
                or not self.canvas.editable or self.canvas.is_editing):
            self.discard_pending()
            self.sync()
            return
        pending, self._pending = self._pending, {}
        self._applying = True
        try:
            if all(field.hasAcceptableInput() for field in (self.horizontal, self.vertical)):
                self.canvas.call(lambda: self._commit("spacing", pending))
        finally:
            self._applying = False
            self.sync()

    def _document_changing(self, event):
        if event.kind == "saved":
            return
        if not self._applying:
            self.discard_pending()
        self._close_popups()

    def _mode_changed(self, *_):
        self.discard_pending()
        self._close_popups()
        self.sync()

    def _sync_presets(self):
        """由有效间距及当前草稿推导预设，避免显示舍入将自定义值误判成预设。"""
        config = self.canvas.document_view.layout
        gaps = None
        if config.algorithm_id == "tree":
            values = dict(zip(("level_gap", "sibling_gap"), resolve_tree_spacing(config)))
            values.update(self._pending)
            gaps = values["level_gap"], values["sibling_gap"]
        self.preset_group.setExclusive(False)
        for button, (_, values) in zip(self.preset_buttons, self.SPACING_PRESETS):
            button.setChecked(gaps == values)
        self.preset_group.setExclusive(True)

    def _commit(self, key, value):
        """整图操作始终使用根节点，不读取当前选择、聚焦节点或属性面板的局部范围。"""
        if key == "wheel":
            self.canvas.set_wheel_mode(value)
            return
        engine = self.canvas.engine
        generation = engine.document_generation
        # 独立宿主可以同时展示设置与属性面板；先提交原节点有效草稿，避免全局修改的
        # document_changing 通知将其丢弃。随后重新读取快照，保留刚提交的节点属性。
        self.canvas.edits_flush_requested.emit()
        if self._disposed or generation != engine.document_generation:
            return
        if not self.canvas.editable or self.canvas.is_editing:
            raise MindMapError("请先结束文字编辑并退出只读模式再修改导图设置")
        page_id = getattr(self.canvas, "current_page_id", None)
        document = engine.page_document(page_id)
        candidate = engine.page_document(page_id)
        info = self.canvas.layout_registry.info(document.layout.algorithm_id)
        if key == "direction":
            if value not in info.directions:
                raise MindMapError("当前布局算法不支持该布局方向")
            candidate.layout = replace(document.layout, direction=value)
        elif key == "compact":
            if not info.supports_compact:
                raise MindMapError("当前布局算法不支持紧凑布局")
            candidate.layout = replace(document.layout, compact=value)
        elif key == "spacing":
            if document.layout.algorithm_id != "tree":
                raise MindMapError("当前布局算法不支持主题间距")
            current = resolve_tree_spacing(document.layout)
            options = dict(zip(("level_gap", "sibling_gap"), current))
            options.update(value)
            if tuple(options.values()) == current:
                return
            # 两个参数始终成对写入，方向切换不改变疏密；compact 仅保留兼容标记。
            candidate.layout = replace(document.layout, options=options,
                                       compact=tuple(options.values()) == (32.0, 10.0))
        elif key == "theme":
            if value not in ("light", "dark"):
                raise MindMapError("请选择浅色或深色主题")
            candidate.theme = Theme() if value == "light" else Theme.dark()
        elif key == "branch":
            if not info.supports_branch_styles:
                raise MindMapError("当前布局算法不支持分支连接线样式")
            apply_branch_style(candidate, candidate.root_ids, value, scope="subtree")
        if candidate == document:
            return
        # 先验证整份候选文档，包括折叠和聚焦范围外的节点，再走公开引擎事务。
        # 校验、布局或前置钩子拒绝时不会留下部分修改，也不会产生撤销记录。
        self.canvas.validate_document(candidate)
        labels = {
            "direction": "修改全局布局方向",
            "compact": "修改紧凑布局",
            "spacing": "修改全局主题间距",
            "theme": "切换主题",
            "branch": "统一全局分支样式",
        }
        with engine.transaction(labels[key]):
            if key in ("direction", "compact", "spacing"):
                engine.set_layout(candidate.layout, page_id=page_id)
            elif key == "theme":
                engine.set_theme(candidate.theme, page_id=page_id)
            else:
                engine.set_branch_style(document.root_ids, value, scope="subtree")

    def sync(self, *events):
        """从正式状态同步值、混合状态及布局能力；不创建默认配置或触发用户选择信号。"""
        if self._disposed or self._applying or (events and getattr(events[0], "kind", None) == "saved"):
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
                self.direction.set_value(document.layout.direction)
                self.compact.setChecked(document.layout.compact)
            spacing_supported = document.layout.algorithm_id == "tree"
            if spacing_supported and not self._pending:
                gaps = resolve_tree_spacing(document.layout)
                down = document.layout.direction == "down"
                for field, value, minimum in (
                    (self.horizontal, gaps[1] if down else gaps[0], 4 if down else 24),
                    (self.vertical, gaps[0] if down else gaps[1], 24 if down else 4),
                ):
                    with QSignalBlocker(field):
                        field.setRange(minimum, 500)
                        field.setValue(value)
            self._sync_presets()
            if not spacing_supported:
                self.preset_group.setExclusive(False)
                for button in self.preset_buttons:
                    button.setChecked(False)
                self.preset_group.setExclusive(True)
            for index in range(self.direction.count()):
                self.direction.model().item(index).setEnabled(
                    self.direction.itemData(index) in info.directions
                )
            kinds = self.canvas.branch_kinds(document.root_ids)
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
                (self.theme, True, "导图主题"),
                (self.horizontal, spacing_supported, "水平主题间距"),
                (self.vertical, spacing_supported, "竖直主题间距"),
                *((button, spacing_supported, f"{name}：父子间距 {gaps[0]:g}，同级间距 {gaps[1]:g}")
                  for button, (name, gaps) in zip(self.preset_buttons, self.SPACING_PRESETS)),
            ):
                field.setEnabled(editable and supported)
                field.setToolTip(
                    f"当前布局算法不支持{label}"
                    if not supported
                    else "文字编辑期间不能修改导图设置"
                    if self.canvas.is_editing
                    else "只读模式不能修改导图设置"
                    if not self.canvas.editable
                    else self.branch.currentText() + "；应用于整张导图，覆盖已有局部连接线样式。"
                    if field is self.branch
                    else f"布局结构：{self.direction.currentText()}" if field is self.direction
                    else label + "（导图逻辑像素）" if field in (self.horizontal, self.vertical)
                    else label
                )
        finally:
            self._loading = False
        for menu in self._menus:
            self._sync_menu(menu)

    def set_ui_colors(self, colors: UiColors | None) -> None:
        """设置独立界面色板；None 恢复宿主文档主题，不提交任何设置或历史。

        非 UiColors 参数抛出 TypeError；滚轮菜单与布局、外观侧栏共用此配色入口。
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
            f"QFrame#settings_separator {{ background: {colors.border}; }}"
            + control_styles(colors)
            + f'QToolButton[spacing_preset="true"] {{ background: {colors.field}; '
              f'border: 1px solid {colors.border}; border-radius: 6px; padding: 4px 8px; }}'
            + f'QToolButton[spacing_preset="true"]:hover {{ background: {colors.hover}; }}'
            + f'QToolButton[spacing_preset="true"]:checked {{ background: {colors.selected}; '
              f'border-color: {colors.accent}; }}'
            + f'QToolButton[spacing_preset="true"]:disabled {{ color: {colors.muted}; }}'
            + menu_styles(colors)
            + scrollbar_styles(colors)
        )
        apply_ui_palette(self, colors, window=surface)
        self.direction.set_theme(theme)
        for field, icons in (
            (self.wheel, ("wheel_scroll", "wheel_zoom")),
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
        for menu in self._menus:
            self._sync_menu(menu)

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
        self.discard_pending()
        self._close_popups()
