"""宿主无关的节点属性与亿图粘贴弹窗；只收集选择，由画布负责提交文档。"""

from dataclasses import dataclass

from mindmap_core import BranchStyle, Theme, get_branch_style
from PySide6.QtCore import QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .properties import NodePropertiesForm, normalize_property
from .ui_style import apply_ui_palette, dialog_styles, ui_colors


@dataclass
class NodePropertiesResult:
    """节点外观补丁和独立的分支批量请求；调用者负责一次性验证、提交。"""

    patch: dict
    branch_kind: BranchStyle | None
    branch_scope: str
    branch_changed: bool


@dataclass(frozen=True)
class EdrawPasteOptions:
    """一次粘贴的用户选择，记忆和事务由调用者处理。

    mode 为 append 或 replace；keep_children 控制是否保留目标原有后代。
    replace_root 仅在替换模式生效，根节点指当前选中的目标主题；False 时保留
    目标文字及文字格式，仅导入来源根的子树。默认 True 兼容已有的二参数调用。
    """

    mode: str
    keep_children: bool
    replace_root: bool = True


def _accent_text(accent):
    """为强调色底选择对比度更高的深／浅文字，兼容浅色和深色界面的品牌色。"""
    def luminance(color):
        values = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in (color.redF(), color.greenF(), color.blueF())]
        return sum(value * weight for value, weight in zip(values, (0.2126, 0.7152, 0.0722)))

    background = luminance(QColor(accent))
    dark = "#18202c"
    contrast_dark = (background + 0.05) / (luminance(QColor(dark)) + 0.05)
    contrast_light = 1.05 / (background + 0.05)
    return dark if contrast_dark >= contrast_light else "#ffffff"


class _PasteChoiceCard(QAbstractButton):
    """整行可点击的互斥选项，以底色、粗边框和文字标签共同表达选中状态。

    仍由 QAbstractButton/QButtonGroup 维护点击、空格、方向键和无障碍状态。
    绘制使用逻辑坐标和真实字体度量，不缩放位图，也不依赖 Windows 原生单选标记，
    避免系统样式与 QSS 混用时圆点消失；高 DPI 的像素比由 Qt 自动处理。
    """

    def __init__(self, title, detail, colors, parent=None):
        super().__init__(parent)
        self.colors, self.detail = colors, detail
        apply_ui_palette(self, colors)
        self.setText(title)
        self.setAccessibleName(title)
        self.setAccessibleDescription(detail)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toggled.connect(self.update)

    def sizeHint(self):
        """按字体保留标题和最多两行说明的空间，不用固定像素字号挤压高 DPI 文字。"""
        metrics = QFontMetrics(self.font())
        return QSize(440, max(80, metrics.height() * 3 + 38))

    def event(self, event):
        if event.type() in (QEvent.Type.HoverEnter, QEvent.Type.HoverLeave):
            self.update()
        return super().event(event)

    def paintEvent(self, event):
        """直接绘制卡片与“已选择”徽标，让状态在不同主题、缩放比例下保持可见。"""
        colors = self.colors
        checked, enabled = self.isChecked(), self.isEnabled()
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        background = colors.selected if checked else colors.hover if self.underMouse() and enabled else colors.field
        border = colors.accent if checked or self.hasFocus() else colors.border
        painter.setBrush(QColor(background))
        painter.setPen(QPen(QColor(border), 2 if checked else 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 8, 8)
        text_color = colors.text if enabled else colors.muted
        title_font = QFont(self.font())
        title_font.setBold(True)
        title_metrics = QFontMetrics(title_font)
        title_width = self.width() - 32
        if checked:
            # 明确显示文字状态，不能只用颜色或一个几像素大的圆点区分选项。
            badge_width = self.fontMetrics().horizontalAdvance("已选择") + 20
            badge = QRectF(self.width() - badge_width - 16, 12, badge_width, title_metrics.height() + 8)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colors.accent))
            painter.drawRoundedRect(badge, 5, 5)
            painter.setPen(QColor(_accent_text(colors.accent)))
            painter.setFont(self.font())
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "已选择")
            title_width -= badge_width + 12
        painter.setPen(QColor(text_color))
        painter.setFont(title_font)
        painter.drawText(QRectF(16, 16, title_width, title_metrics.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text())
        painter.setFont(self.font())
        painter.drawText(QRectF(16, title_metrics.height() + 23, self.width() - 32,
                               self.height() - title_metrics.height() - 35),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                         self.detail)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(colors.accent), 1, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(5, 5, -5, -5), 5, 5)


def edraw_paste_options(
    parent, *, target_text, root_count, node_count, mode="append", keep_children=True,
    replace_root=True, theme=None
):
    """显示亿图粘贴选项，确认返回选项，取消返回 None；不会修改文档或本机偏好。

    多棵来源树只能追加，界面暂时禁用替换；调用者须保留先前的单树粘贴偏好。
    根节点与后代处理相互独立，replace_root 默认替换当前主题文字，关闭后仅导入子树。
    追加模式隐藏并禁用这两组处理选项，但保留内存中的选择，切回替换时继续恢复。
    theme 接受当前组件的界面色板或导图主题，使独立画布及桌面宿主均保持局部配色。
    """
    dialog = QDialog(parent)
    dialog.setObjectName("edraw_paste_dialog")
    dialog.setWindowTitle("粘贴亿图脑图")
    dialog.setMinimumWidth(480)
    # 仅调整本弹窗的中文界面字体和点字号；保持真实文本绘制，避免拉伸截图式控件。
    font = QFont(dialog.font())
    font.setFamilies(["Microsoft YaHei UI", "微软雅黑", font.family()])
    font.setPointSizeF(max(10, font.pointSizeF()))
    dialog.setFont(font)
    colors = ui_colors(theme or Theme())
    accent_text = _accent_text(colors.accent)
    dialog.setStyleSheet(
        dialog_styles(colors)
        + 'QPushButton[edraw_paste_choice="true"] { padding: 7px 12px; border-width: 2px; }'
        + f'QPushButton[edraw_paste_choice="true"]:checked {{ background: {colors.accent}; '
          f'color: {accent_text}; border-color: {colors.accent}; font-weight: 600; }}'
        + f'QPushButton#edraw_paste_confirm {{ background: {colors.accent}; color: {accent_text}; '
          f'border-color: {colors.accent}; font-weight: 600; }}'
    )
    apply_ui_palette(dialog, colors)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 20, 20, 16)
    layout.setSpacing(12)
    target = target_text.replace("\n", " ")
    target = target[:120] + "…" if len(target) > 120 else target
    description = QLabel(f"目标节点：{target}", dialog)
    # 主题正文是用户数据，必须按纯文本显示，避免尖括号被 QLabel 当作 HTML 解释。
    description.setTextFormat(Qt.TextFormat.PlainText)
    description.setWordWrap(True)
    layout.addWidget(description)
    source = QLabel(f"来源：{root_count} 个顶层主题，共 {node_count} 个节点", dialog)
    layout.addWidget(source)
    append = _PasteChoiceCard("追加为子主题", "在目标节点下添加复制内容，保留现有内容。", colors, dialog)
    append.setObjectName("edraw_append")
    replace = _PasteChoiceCard(
        "替换当前主题", "用复制内容更新当前子树，可选择保留根节点和原有后代。"
        if root_count == 1 else "复制多个并列主题时不可替换，请选择追加。", colors, dialog,
    )
    replace.setObjectName("edraw_replace")
    modes = QButtonGroup(dialog)
    modes.addButton(append)
    modes.addButton(replace)
    replace.setEnabled(root_count == 1)
    replace.setChecked(root_count == 1 and mode == "replace")
    append.setChecked(not replace.isChecked())
    layout.addWidget(append)
    layout.addWidget(replace)
    # 两组按钮各自互斥，根节点与原有后代的选择互不影响；复用文字按钮的高亮样式，
    # 让状态在系统明暗主题及高 DPI 下均清晰可见，不依赖原生单选标记。
    root_options = QWidget(dialog)
    root_options.setObjectName("edraw_root_options")
    root_layout = QVBoxLayout(root_options)
    root_layout.setContentsMargins(0, 0, 0, 0)
    root_layout.setSpacing(8)
    root_layout.addWidget(QLabel("根节点如何处理（当前选中的主题）", root_options))
    root_buttons = QHBoxLayout()
    root_buttons.setSpacing(8)
    root_group = QButtonGroup(dialog)
    replace_root_button = QPushButton("替换根节点", root_options)
    replace_root_button.setObjectName("edraw_replace_root")
    replace_root_button.setAccessibleDescription("用复制内容的根主题文字替换当前主题文字。")
    keep_root = QPushButton("保留根节点", root_options)
    keep_root.setObjectName("edraw_keep_root")
    keep_root.setAccessibleDescription("保留当前主题文字和格式，仅导入复制内容的子树。")
    for button in (replace_root_button, keep_root):
        button.setCheckable(True)
        button.setAutoDefault(False)
        button.setAccessibleName(button.text())
        button.setProperty("edraw_paste_choice", True)
        root_group.addButton(button)
        root_buttons.addWidget(button)
    replace_root_button.setChecked(replace_root)
    keep_root.setChecked(not replace_root)
    root_layout.addLayout(root_buttons)
    layout.addWidget(root_options)
    descendants = QWidget(dialog)
    descendants.setObjectName("edraw_descendants")
    descendants_layout = QVBoxLayout(descendants)
    descendants_layout.setContentsMargins(0, 0, 0, 0)
    descendants_layout.setSpacing(8)
    descendants_layout.addWidget(QLabel("原有后代如何处理", descendants))
    history_buttons = QHBoxLayout()
    history_buttons.setSpacing(8)
    history_group = QButtonGroup(dialog)
    keep = QPushButton("保留原有后代", descendants)
    keep.setObjectName("edraw_keep_children")
    remove = QPushButton("移除原有后代", descendants)
    remove.setObjectName("edraw_remove_children")
    for button in (keep, remove):
        button.setCheckable(True)
        button.setAutoDefault(False)
        button.setProperty("edraw_paste_choice", True)
        history_group.addButton(button)
        history_buttons.addWidget(button)
    keep.setChecked(keep_children)
    remove.setChecked(not keep_children)
    descendants_layout.addLayout(history_buttons)
    result_hint = QLabel(descendants)
    result_hint.setObjectName("edraw_paste_result_hint")
    result_hint.setWordWrap(True)
    descendants_layout.addWidget(result_hint)
    layout.addWidget(descendants)
    hint = QLabel(
        "多个并列主题仅支持追加。下次粘贴单棵树时恢复之前的选择。"
        if root_count > 1 else "选择会自动记忆，下次粘贴时仍会询问。",
        dialog,
    )
    hint.setWordWrap(True)
    layout.addWidget(hint)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
    confirm.setObjectName("edraw_paste_confirm")
    confirm.setDefault(True)
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    def sync_choices():
        """隐藏不适用的根节点和后代选项，组合预告结果，切换方式时保留两组选择。"""
        replacing = replace.isChecked()
        root_options.setEnabled(replacing)
        root_options.setVisible(replacing)
        descendants.setEnabled(replacing)
        descendants.setVisible(replacing)
        confirm.setText("替换粘贴" if replacing else "追加粘贴")
        root_summary = ("使用复制内容替换当前主题文字。" if replace_root_button.isChecked()
                        else "保留当前主题文字和格式，仅导入复制内容的子树。")
        descendants_summary = ("保留已有分支，新分支追加在后。" if keep.isChecked()
                               else "移除已有分支，使用复制内容的子树；可通过撤销恢复。")
        result_hint.setText(root_summary + "\n" + descendants_summary)
        if dialog.isVisible():
            dialog.adjustSize()

    replace.toggled.connect(sync_choices)
    replace_root_button.toggled.connect(sync_choices)
    keep.toggled.connect(sync_choices)
    sync_choices()
    # 显示前将初始焦点交给实际选中的粘贴方式，避免 Qt 默认聚焦首项，
    # 造成未选项的焦点虚线与另一项的选中高亮同时出现；多树来源会聚焦追加项。
    modes.checkedButton().setFocus()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return EdrawPasteOptions(
        "replace" if replace.isChecked() else "append", keep.isChecked(), replace_root_button.isChecked()
    )


def node_properties(
    parent, node, *, branch_styles=True, branch_scope="subtree", theme=None, root_id=None, parents=None
):
    """返回待提交外观；取消返回 None，未修改的分支不会覆盖后代已有配置。

    root_id、parents 由画布提供，用于根主题默认字体和一级分支方向的编辑资格。
    只收集实际修改的样式、图片宽度及方向，正文、备注等内容字段不进入补丁。
    """
    dialog = QDialog(parent)
    dialog.setObjectName("node_properties_dialog")
    dialog.setWindowTitle("节点属性")
    dialog.resize(390, 720)
    palette = theme or Theme()
    # 表单之外的留白与确定／取消按钮也跟随文档主题，浮层不依赖系统明暗配色。
    colors = ui_colors(palette)
    dialog.setStyleSheet(dialog_styles(colors))
    apply_ui_palette(dialog, colors)
    layout = QVBoxLayout(dialog)
    form = NodePropertiesForm(dialog)
    form.set_theme(palette)
    form.load_nodes(
        [node], root_id=root_id, parents=parents, branch_styles=branch_styles, branch_scope=branch_scope
    )
    baseline = form.values()
    layout.addWidget(form)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    values = form.values()
    patch, style = {}, dict(node.style)
    for key, value in values.items():
        if key == "branch.kind" or value == baseline[key]:
            continue
        value = normalize_property(key, value)
        if key.startswith("style."):
            name = key.split(".")[1]
            if value == "":
                style.pop(name, None)
            else:
                style[name] = value
        else:
            patch[key] = value
    if style != node.style:
        patch["style"] = style
    selected = BranchStyle(values["branch.kind"]) if values["branch.kind"] else None
    return NodePropertiesResult(
        patch, selected, form.scope.currentData(), branch_styles and selected != get_branch_style(node.style)
    )
