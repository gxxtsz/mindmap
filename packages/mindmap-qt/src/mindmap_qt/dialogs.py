"""兼容独立画布的属性弹窗；与工作区侧栏复用同一个节点表单。"""

from dataclasses import dataclass

from mindmap_engine import BranchStyle, Theme, get_branch_style
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout

from .properties import NodePropertiesForm, normalize_property
from .ui_style import apply_ui_palette, dialog_styles, ui_colors


@dataclass
class NodePropertiesResult:
    """节点外观补丁和独立的分支批量请求；调用者负责一次性验证、提交。"""

    patch: dict
    branch_kind: BranchStyle | None
    branch_scope: str
    branch_changed: bool


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
