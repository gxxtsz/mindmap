"""与停靠框架无关的思维导图面板组合及选择、输入生命周期。"""

from mindmap_engine import MindMapError
from mindmap_qt_ui.ui_style import UiColors
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QWidget

from .contracts import PanelHost
from .inspector import NodePropertiesPanel
from .outline import OutlinePanel
from .settings import GlobalSettingsPanel


class MindMapPanels(QObject):
    """为一个画布创建三个可交给任意宿主承载的业务面板。

    面板 QWidget 的父对象由宿主布局或停靠管理器接管；本对象保留协调逻辑，
    不重新设置已注册面板的 parent。dispose() 取消草稿、弹层和信号连接，
    不销毁画布或面板。宿主真正退出时再调用 canvas.dispose()。
    """

    def __init__(self, canvas: PanelHost, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self._disposed = False
        # 延迟工具工厂尚未激活时，面板放在画布拥有的隐藏暂存容器里。
        # 只隐藏容器，不显式隐藏面板；宿主布局接管后 Qt 能正常显示内容，
        # 未被激活的面板也不会成为无主顶层窗口或覆盖画布。
        self._parking = QWidget(canvas if isinstance(canvas, QWidget) else None)
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
            panel.set_theme(colors or self.canvas.document_view.theme)

    def dispose(self):
        """幂等停止提交和弹层；关闭停靠面板时不应调用，退出宿主时才释放。"""
        if self._disposed:
            return
        self._disposed = True
        self.properties.dispose()
        self.settings.dispose()
        self.outline.nodes_selected.disconnect(self._outline_selected)
        self.canvas.edits_flush_requested.disconnect(self.flush_pending_edits)

    def reveal_nodes(self, ids):
        """通过公开宿主协议定位大纲选择；失败时恢复选择显示并发出错误信号。

        展开祖先、聚焦退出及视口定位归宿主管理，面板不持有场景或节点图元。
        宿主在定位前通过 edits_flush_requested 请求提交有效输入，保持原子事务边界。
        """
        if self._disposed:
            return
        try:
            self.canvas.reveal_nodes(ids)
        except (MindMapError, KeyError) as exc:
            self.canvas.error_occurred.emit(str(exc))
            self.outline.sync_selection(self.canvas.selected_node_ids())

    def _outline_selected(self, ids):
        """保留原有大纲回调名称，统一委托公开定位接口。"""
        self.reveal_nodes(ids)
