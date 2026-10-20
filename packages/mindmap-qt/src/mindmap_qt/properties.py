"""单页节点样式表单：字体、主题、分支共用一个滚动区，供侧栏和弹窗复用。"""

from __future__ import annotations

from mindmap_engine import Theme, get_branch_style
from PySide6.QtCore import QEvent, QPointF, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPalette, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyleOptionSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .branch_styles import populate_branch_combo
from .icons import _toolbar_icon
from .property_controls import MIXED, BranchPicker, ColorButton, FlowLayout, FontPicker, ToggleButton
from .ui_style import ComboBox, apply_ui_palette, control_styles, menu_styles, scrollbar_styles, ui_colors

STYLE_DEFAULTS = {
    "font_family": "",
    "font_size": 11,
    "width": 280,
    "bold": False,
    "italic": False,
    "underline": False,
    "text_color": "",
    "background": "",
    "border_color": "",
    "border_width": 1,
}


class _CheckBox(QCheckBox):
    """混合值只用于展示；用户点击后只在明确的真、假之间切换。"""

    def nextCheckState(self):
        self.setCheckState(
            Qt.CheckState.Unchecked
            if self.isChecked() and self.checkState() != Qt.CheckState.PartiallyChecked
            else Qt.CheckState.Checked
        )


class _Number(QDoubleSpinBox):
    """保留数值与混合占位的区别，不能将刷新时的内部保留值误当成用户输入。"""

    mixed = False

    def textFromValue(self, value):
        """整数省略末尾小数，保留小数编辑能力，给窄侧栏和高 DPI 的字号留足空间。"""
        text = super().textFromValue(value)
        suffix = self.locale().decimalPoint() + "0" * self.decimals()
        return text.removesuffix(suffix) if self.decimals() else text

    def paintEvent(self, event):
        """显式绘制上下箭头，避免原生 Windows 样式在定制纵向按钮后省略箭头。

        命中区域仍由 QDoubleSpinBox 和样式子控件维护；这里只绘制三角形，不替换
        键盘、步进、长按或数值校验逻辑。坐标使用逻辑像素，由 Qt 处理高 DPI。
        """
        super().paintEvent(event)
        option = QStyleOptionSpinBox()
        self.initStyleOption(option)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for control, up in (
            (QStyle.SubControl.SC_SpinBoxUp, True),
            (QStyle.SubControl.SC_SpinBoxDown, False),
        ):
            rect = self.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, control, self)
            enabled = self.isEnabled() and (
                self.value() < self.maximum() if up else self.value() > self.minimum()
            )
            group = QPalette.ColorGroup.Active if enabled else QPalette.ColorGroup.Disabled
            painter.setBrush(self.palette().color(group, QPalette.ColorRole.Text))
            center, sign = rect.center(), -1 if up else 1
            painter.drawPolygon(
                QPolygonF(
                    [
                        QPointF(center.x() - 3, center.y() - sign * 1.5),
                        QPointF(center.x() + 3, center.y() - sign * 1.5),
                        QPointF(center.x(), center.y() + sign * 2),
                    ]
                )
            )

    def set_mixed(self, mixed):
        self.mixed = mixed
        self.lineEdit().setPlaceholderText("多种值" if mixed else "")
        if mixed:
            self.lineEdit().clear()


class NodePropertiesForm(QWidget):
    """共享的单页样式表单，不访问引擎、不修改节点内容。

    edited 的第三个参数区分离散选项和连续输入，提交时机由宿主控制器决定。
    load_nodes、主题更新及混合值展示不发送修改信号；values 供独立弹窗比较基线，
    因而侧栏自动提交与弹窗确定提交共用同一套值语义和未配置状态。
    """

    edited = Signal(str, object, bool)
    flush_requested = Signal()
    scope_changed = Signal(str)
    composition_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("node_properties_form")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.fields, self.errors, self.sections = {}, {}, {}
        self._loading = False
        self._editable = False
        self._composing = set()
        self._theme_signature = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.summary = QLabel("请选择节点", self)
        self.summary.setObjectName("property_summary")
        self.summary.setWordWrap(True)
        self.summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.summary.setContentsMargins(12, 4, 12, 4)
        self.summary.setMinimumHeight(26)
        self.summary.setMaximumHeight(54)
        outer.addWidget(self.summary)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.page = QWidget()
        self.page.setObjectName("properties_page")
        self.page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.scroll.setWidget(self.page)
        outer.addWidget(self.scroll, 1)
        self.body = QVBoxLayout(self.page)
        self.body.setContentsMargins(12, 0, 12, 16)
        self.body.setSpacing(12)
        self._build_font()
        self._build_theme()
        self._build_branches()
        self.body.addStretch(1)
        self.set_theme(Theme())

    def _section(self, name):
        """分组只提供标题和分隔线，始终展开，所有字段共用外层滚动位置。"""
        section = QWidget(self.page)
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        if self.sections:
            line = QFrame(section)
            line.setObjectName("property_separator")
            line.setFixedHeight(1)
            layout.addWidget(line)
        title = QLabel(name, section)
        title.setProperty("property_heading", True)
        title.setMinimumHeight(32)
        layout.addWidget(title)
        self.sections[name] = section
        self.body.addWidget(section)
        return layout

    def _build_font(self):
        section = self._section("字体")
        row = QWidget()
        flow = FlowLayout(row)
        flow.addWidget(self._register("style.font_family", "字体", FontPicker(), grow=True))
        flow.addWidget(self._register("style.font_size", "字号", self._number(6, 96, width=66)))
        for name, label, delta in (("increase_size", "增大字号", 1), ("decrease_size", "减小字号", -1)):
            button = QToolButton()
            button.setObjectName(name)
            button.setFixedSize(32, 34)
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.clicked.connect(lambda checked=False, step=delta: self._step_font(step))
            setattr(self, name, button)
            flow.addWidget(button)
        section.addWidget(row)
        tools = QHBoxLayout()
        tools.setSpacing(8)
        for key, label in (("bold", "加粗"), ("italic", "斜体"), ("underline", "下划线")):
            tools.addWidget(self._register("style." + key, label, ToggleButton(label)))
        tools.addWidget(
            self._register("style.text_color", "文字颜色", ColorButton("文字颜色", text_color=True))
        )
        tools.addStretch(1)
        section.addLayout(tools)

    def _build_theme(self):
        section = self._section("主题")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(10)
        section.addLayout(form)
        for key, label in (("background", "填充颜色"), ("border_color", "边框颜色")):
            self._row(form, label, self._register("style." + key, label, ColorButton(label), right=True))
        self._row(
            form,
            "边框宽度",
            self._register("style.border_width", "边框宽度", self._number(0, 10), right=True),
        )
        self.width_enabled = _CheckBox("自定义最大文本宽度")
        self.width_enabled.setObjectName("custom_width")
        self.width_enabled.setAccessibleName("自定义最大文本宽度")
        self.width_enabled.setToolTip("关闭后恢复默认最大文本宽度，不改变其他样式")
        self._row(
            form,
            self.width_enabled,
            self._register("style.width", "最大文本宽度", self._number(40, 1200), right=True),
        )
        self.width_enabled.checkStateChanged.connect(self._width_toggled)
        self._row(
            form,
            "图片显示宽度",
            self._register("image_width", "图片显示宽度", self._number(20, 1200), right=True),
        )
        direction = ComboBox()
        direction.setMinimumHeight(34)
        direction.setMaximumWidth(104)
        for label, value in (("自动", None), ("左侧", "left"), ("右侧", "right")):
            direction.addItem(label, value)
        self._row(form, "一级分支方向", self._register("direction", "一级分支方向", direction, right=True))

    def _build_branches(self):
        section = self._section("分支")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(10)
        section.addLayout(form)
        kind = BranchPicker()
        kind.setObjectName("branch_style")
        self._row(form, "连接线样式", self._register("branch.kind", "连接线样式", kind, right=True))
        self.scope = ComboBox()
        self.scope.setObjectName("branch_scope")
        self.scope.setAccessibleName("分支样式作用范围")
        self.scope.setMinimumHeight(34)
        self.scope.setMaximumWidth(130)
        self.scope.addItem("整个子树", "subtree")
        self.scope.addItem("仅本级", "level")
        form.addRow("作用范围", self.scope)
        form.setAlignment(self.scope, Qt.AlignmentFlag.AlignRight)
        self.scope.currentIndexChanged.connect(self._scope_edited)

    @staticmethod
    def _row(form, label, field):
        if isinstance(label, str):
            label = QLabel(label)
            label.setWordWrap(True)
        form.addRow(label, field)

    @staticmethod
    def _number(low, high, *, width=104):
        field = _Number()
        field.setRange(low, high)
        field.setDecimals(1)
        field.setMinimumSize(min(66, width), 34)
        field.setMaximumWidth(width)
        return field

    def _register(self, key, label, field, *, grow=False, right=False):
        """为控件附加字段标识及错误提示；所有实际编辑统一进入 edited 信号。"""
        self.fields[key] = field
        if not field.objectName():
            field.setObjectName(key.replace(".", "_"))
        field.setAccessibleName(label)
        container = QWidget()
        container.setSizePolicy(
            QSizePolicy.Policy.Expanding if grow else QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        if not right and not grow:
            container.setMaximumWidth(field.maximumWidth())
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        error = QLabel(container)
        error.setWordWrap(True)
        error.setObjectName("property_error")
        error.hide()
        self.errors[key] = error
        layout.addWidget(field, 0, Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag(0))
        layout.addWidget(error)
        if isinstance(field, FontPicker):
            field.changed.connect(lambda value, immediate, k=key: self._emit(k, value, immediate))
            self._watch_input(field.lineEdit(), key)
        elif isinstance(field, ToggleButton):
            field.changed.connect(lambda value, k=key: self._emit(k, value, True))
        elif isinstance(field, ColorButton):
            field.changed.connect(lambda value, k=key: self._emit(k, value, True))
        elif isinstance(field, QComboBox):
            field.activated.connect(lambda index, k=key, f=field: self._emit(k, f.itemData(index), True))
        elif isinstance(field, QDoubleSpinBox):
            field.valueChanged.connect(lambda value, k=key: self._number_changed(k, value))
            self._watch_input(field.lineEdit(), key)
            # 用户输入可能与混合状态下保留的内部数值相同；此时仍需发出明确修改。
            field.lineEdit().textEdited.connect(lambda text, k=key, f=field: self._number_edited(k, f, text))
        self._watch_input(field, key)
        return container

    def _watch_input(self, field, key):
        field.setProperty("property_key", key)
        field.installEventFilter(self)

    def _emit(self, key, value, immediate):
        if self._loading or key in self._composing or not self.fields[key].isEnabled():
            return
        self.set_error(key, "")
        self.edited.emit(key, value, immediate)

    def _number_changed(self, key, value):
        if self._loading:
            return
        self.fields[key].set_mixed(False)
        if key == "style.width" and self.fields[key].isEnabled():
            with QSignalBlocker(self.width_enabled):
                self.width_enabled.setCheckState(Qt.CheckState.Checked)
        self._sync_font_buttons()
        self._emit(key, value, False)

    def _number_edited(self, key, field, text):
        value, valid = field.locale().toDouble(text)
        if valid and field.minimum() <= value <= field.maximum():
            self._number_changed(key, value)

    def _width_toggled(self, state):
        """宽度开关只修改 width 一个键；关闭表示删除配置，不能写入占位默认值。"""
        if self._loading or not self._editable or state == Qt.CheckState.PartiallyChecked:
            return
        field = self.fields["style.width"]
        enabled = state == Qt.CheckState.Checked
        field.setEnabled(enabled)
        if enabled and field.mixed:
            with QSignalBlocker(field):
                field.setValue(STYLE_DEFAULTS["width"])
            field.set_mixed(False)
        self.set_error("style.width", "")
        self.edited.emit("style.width", field.value() if enabled else "", True)

    def _step_font(self, delta):
        field = self.fields["style.font_size"]
        if not field.isEnabled() or field.mixed:
            return
        with QSignalBlocker(field):
            field.setValue(field.value() + delta)
        self._sync_font_buttons()
        self._emit("style.font_size", field.value(), True)

    def _sync_font_buttons(self):
        field = self.fields["style.font_size"]
        enabled = field.isEnabled() and not field.mixed
        self.increase_size.setEnabled(enabled and field.value() < field.maximum())
        self.decrease_size.setEnabled(enabled and field.value() > field.minimum())

    def _scope_edited(self):
        if not self._loading:
            self.scope_changed.emit(self.scope.currentData())

    def eventFilter(self, watched, event):
        key = watched.property("property_key")
        if key and event.type() == QEvent.Type.InputMethod:
            if event.preeditString():
                self._composing.add(key)
            else:
                was_composing = key in self._composing
                self._composing.discard(key)
                if was_composing:
                    # 候选取消也可能没有 textEdited；事件结束后恢复等待周期。
                    QTimer.singleShot(0, self.composition_finished.emit)
        elif key and event.type() == QEvent.Type.FocusOut and not self._loading:
            self.flush_requested.emit()
        return super().eventFilter(watched, event)

    @property
    def composing(self):
        """输入法候选未确认时，控制器不能提交当前输入会话中的缓存值。"""
        return bool(self._composing)

    def set_error(self, key, message):
        """只标记失败字段；文档和历史的回滚由提交控制器负责。"""
        field, label = self.fields[key], self.errors[key]
        field.setProperty("invalid", bool(message))
        field.style().unpolish(field)
        field.style().polish(field)
        label.setText(message)
        label.setVisible(bool(message))

    @staticmethod
    def common(values):
        values = list(values)
        return values[0] if values and all(value == values[0] for value in values) else MIXED

    def _set_value(self, key, value):
        field = self.fields[key]
        if isinstance(field, (FontPicker, ToggleButton, ColorButton, BranchPicker)):
            field.set_value(value)
        elif isinstance(field, QComboBox):
            index = field.findData("__mixed__")
            if index >= 0:
                field.removeItem(index)
            if value is MIXED:
                field.insertItem(0, "多种样式" if key == "branch.kind" else "多种值", "__mixed__")
                field.model().item(0).setEnabled(False)
                field.setCurrentIndex(0)
            else:
                field.setCurrentIndex(max(0, field.findData(value)))
            field.setToolTip(field.currentText())
        elif isinstance(field, QDoubleSpinBox):
            if value is not MIXED:
                field.setValue(value)
            field.set_mixed(value is MIXED)

    def values(self):
        """读取真实输入值供弹窗比较基线；未启用宽度返回删除标记，混合态不转成默认值。"""
        values = {}
        for key, field in self.fields.items():
            if isinstance(field, (FontPicker, ToggleButton, ColorButton, BranchPicker)):
                values[key] = field.value()
            elif isinstance(field, QComboBox):
                values[key] = MIXED if field.currentData() == "__mixed__" else field.currentData()
            else:
                values[key] = MIXED if field.mixed else field.value()
        if self.width_enabled.checkState() == Qt.CheckState.Unchecked:
            values["style.width"] = ""
        return values

    def load_nodes(
        self,
        nodes,
        *,
        root_id=None,
        parents=None,
        editable=True,
        branch_styles=True,
        branch_scope="subtree",
        branch_nodes=None,
    ):
        """以正式快照填充单选或多选，不将默认值、混合值及未配置状态转换成修改请求。"""
        self._loading = True
        self._editable = editable and bool(nodes)
        self._composing.clear()
        try:
            self.summary.setText(
                "请选择节点"
                if not nodes
                else f"已选择 {len(nodes)} 个节点"
                if len(nodes) > 1
                else (nodes[0].text.splitlines() or ["未命名节点"])[0]
            )
            self.scroll.setVisible(bool(nodes))
            self.width_enabled.setEnabled(self._editable)
            self.scope.setEnabled(self._editable and branch_styles)
            for key, field in self.fields.items():
                enabled = self._editable
                if key == "image_width":
                    enabled &= all(node.image_id is not None for node in nodes)
                if key == "direction" and parents is not None:
                    enabled &= all(parents.get(node.id) == root_id for node in nodes)
                if key == "branch.kind":
                    enabled &= branch_styles
                field.setEnabled(enabled)
                self.set_error(key, "")
                if not nodes or key.startswith("branch."):
                    continue
                name = key.split(".")[-1]
                values = [
                    node.style.get(
                        name,
                        13
                        if name == "font_size" and node.id == root_id
                        else True
                        if name == "bold" and node.id == root_id
                        else STYLE_DEFAULTS.get(name),
                    )
                    if key.startswith("style.")
                    else getattr(node, key)
                    for node in nodes
                ]
                self._set_value(key, self.common(values))
            if nodes:
                custom = self.common("width" in node.style for node in nodes)
                self.width_enabled.setTristate(custom is MIXED)
                self.width_enabled.setCheckState(
                    Qt.CheckState.PartiallyChecked
                    if custom is MIXED
                    else Qt.CheckState.Checked
                    if custom
                    else Qt.CheckState.Unchecked
                )
                self.fields["style.width"].setEnabled(self._editable and custom is not False)
                self.scope.setCurrentIndex(max(0, self.scope.findData(branch_scope)))
                self.set_branch_values(branch_nodes if branch_nodes is not None else nodes)
                if not branch_styles:
                    self.fields["branch.kind"].setToolTip("当前布局算法不支持分支连接线样式")
            self._sync_font_buttons()
        finally:
            self._loading = False

    def set_branch_values(self, nodes):
        """范围切换只更新分支展示，保留其他字段中的焦点、光标和待提交输入。"""
        loading, self._loading = self._loading, True
        try:
            self._set_value(
                "branch.kind",
                self.common(kind.value if (kind := get_branch_style(node.style)) else None for node in nodes),
            )
        finally:
            self._loading = loading

    def _surface_color(self, theme: Theme) -> str:
        """共享表单的内部背景钩子；默认保留独立弹窗底色，由侧栏子类覆盖。

        基类初始化时也会调用，覆盖实现不能依赖尚未初始化的画布或编辑状态。
        仅控制容器背景，输入框、补全列表和分支弹层继续使用通用界面色板。
        """
        return ui_colors(theme).surface

    def set_theme(self, theme):
        """主题更新只调整资源和配色，不能改动当前字段值或触发编辑提交。"""
        theme = getattr(self, "_ui_colors_override", None) or theme
        colors = ui_colors(theme)
        surface = self._surface_color(theme)
        signature = (colors, surface)
        if signature == self._theme_signature:
            return
        self._theme_signature = signature
        self.setStyleSheet(
            f"QWidget#node_properties_form, QWidget#properties_page {{ background: {surface}; }}"
            f"QScrollArea, QScrollArea > QWidget > QWidget {{ background: {surface}; }}"
            f"QLabel#property_summary {{ color: {colors.muted}; }}"
            'QLabel[property_heading="true"] { font-size: 15px; font-weight: bold; }'
            f"QFrame#property_separator {{ background: {colors.border}; }}"
            + control_styles(colors)
            + menu_styles(colors)
            + scrollbar_styles(colors)
        )
        apply_ui_palette(self, colors, window=surface)
        for name in ("bold", "italic", "underline"):
            self.fields["style." + name].setIcon(
                _toolbar_icon("font_" + name, colors.text, colors.muted, colors.accent)
            )
        self.increase_size.setIcon(_toolbar_icon("font_increase", colors.text, colors.muted, colors.accent))
        self.decrease_size.setIcon(_toolbar_icon("font_decrease", colors.text, colors.muted, colors.accent))
        # 补全列表是独立弹层，不一定继承表单 QSS；显式着色但不重建字体模型或输入框。
        popup = self.fields["style.font_family"].completer().popup()
        apply_ui_palette(popup, colors)
        popup.setStyleSheet(
            f"QAbstractItemView {{ background: {colors.surface}; color: {colors.text}; "
            f"selection-background-color: {colors.selected}; selection-color: {colors.text}; "
            f"border: 1px solid {colors.border}; }}" + scrollbar_styles(colors)
        )
        for field in self.fields.values():
            if isinstance(field, (ColorButton, BranchPicker)):
                field.set_theme(theme)
        loading, self._loading = self._loading, True
        try:
            combo = self.fields["branch.kind"]
            current = combo.currentData()
            combo.clear()
            populate_branch_combo(combo, theme)
            self._set_value("branch.kind", MIXED if current == "__mixed__" else current)
        finally:
            self._loading = loading


def normalize_property(key, value):
    """把明确的控件输入转为样式值；混合占位和无效颜色不得进入文档。"""
    if value is MIXED or value == "__mixed__":
        raise ValueError("请选择明确的属性值")
    if key in {"style.font_family", "style.text_color", "style.background", "style.border_color"}:
        value = value.strip()
        if key != "style.font_family" and value and not QColor(value).isValid():
            raise ValueError("颜色无效，请选择有效颜色，例如 #3478d5")
    return value
