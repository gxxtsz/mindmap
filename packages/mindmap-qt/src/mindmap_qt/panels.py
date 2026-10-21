"""与停靠框架无关的思维导图面板组合及选择、输入生命周期。"""

from mindmap_engine import MindMapError
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QWidget

from .inspector import NodePropertiesPanel
from .outline import OutlinePanel
from .settings import GlobalSettingsPanel
from .ui_style import UiColors


class MindMapPanels(QObject):
    """为一个画布创建三个可交给任意宿主承载的业务面板。

    面板 QWidget 的父对象由宿主布局或停靠管理器接管；本对象保留协调逻辑，
    不重新设置已注册面板的 parent。dispose() 取消草稿、弹层和信号连接，
    不销毁画布或面板。宿主真正退出时再调用 canvas.dispose()。
    """

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self._disposed = False
        # 延迟工具工厂尚未激活时，面板放在画布拥有的隐藏暂存容器里。
        # 只隐藏容器，不显式隐藏面板；宿主布局接管后 Qt 能正常显示内容，
        # 未被激活的面板也不会成为无主顶层窗口或覆盖画布。
        self._parking = QWidget(canvas)
        self._parking.hide()
        self.outline = OutlinePanel(canvas, self._parking)
        self.properties = NodePropertiesPanel(canvas, self._parking)
        self.settings = GlobalSettingsPanel(canvas, self._parking)
        self.outline.nodes_selected.connect(self._outline_selected)
        canvas.edits_flush_requested.connect(self.flush_pending_edits)

    def flush_pending_edits(self):
        """保存、导出和宿主关闭前，提交已完成的有效输入；输入法候选保持原语义。"""
        if not self._disposed:
            self.properties.flush()

    def set_ui_colors(self, colors: UiColors | None) -> None:
        """指定独立界面色板；None 恢复随文档主题，更新不触发文档编辑。"""
        if colors is not None and not isinstance(colors, UiColors):
            raise TypeError("界面色板必须为 UiColors 或 None")
        for panel in (self.outline, self.properties, self.settings):
            panel._ui_colors_override = colors
            panel.set_theme(colors or self.canvas._document.theme)

    def dispose(self):
        """幂等停止提交和弹层；关闭停靠面板时不应调用，退出宿主时才释放。"""
        if self._disposed:
            return
        self._disposed = True
        self.properties.dispose()
        self.settings.dispose()
        self.outline.nodes_selected.disconnect(self._outline_selected)
        self.canvas.edits_flush_requested.disconnect(self.flush_pending_edits)

    def _outline_selected(self, ids):
        """完整树到画布的导航：必要的展开共用一次事务，聚焦退出属于临时视图状态。"""
        if self._disposed:
            return
        self.flush_pending_edits()
        self.canvas.finish_edit(commit=True)
        document = self.canvas._document
        parents, ancestors = self.canvas._node_parents, set()
        for key in ids:
            parent = parents.get(key)
            while parent is not None:
                ancestors.add(parent)
                parent = parents.get(parent)
        collapsed = [key for key in ancestors if not document.nodes[key].expanded]
        if collapsed and not self.canvas.editable:
            self.canvas.error_occurred.emit("只读模式不能展开隐藏节点，请先退出只读模式")
            self.outline.sync_selection(self.canvas.selected_node_ids())
            return
        try:
            focus = self.canvas.focus_id
            outside_focus = bool(focus) and any(key not in set(document.descendants(focus)) for key in ids)
            if collapsed and not self.canvas._can_incremental():
                candidate = self.canvas.engine.document
                for key in collapsed:
                    candidate.nodes[key].expanded = True
                self.canvas.validate_document(candidate)
            if collapsed:
                with self.canvas.engine.transaction("大纲定位展开"):
                    for key in collapsed:
                        self.canvas.engine.set_expanded(key, True)
            if focus and outside_focus:
                self.canvas.focus_id = None
                self.canvas.refresh()
            self.canvas.select_nodes(ids)
            if ids:
                bounds = self.canvas.node_items[ids[0]].sceneBoundingRect()
                for key in ids[1:]:
                    bounds = bounds.united(self.canvas.node_items[key].sceneBoundingRect())
                self.canvas.view.ensureVisible(bounds, 35, 35)
        except (MindMapError, KeyError) as exc:
            self.canvas.error_occurred.emit(str(exc))
            self.outline.sync_selection(self.canvas.selected_node_ids())

