"""节点专用详情页协议、注册表和页内导航容器；扩展不能注册顶级文档。"""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from importlib.metadata import entry_points

from mindmap_core import MindMapError, PathEntry
from mindmap_core.node_pages import extension_page_id, parse_extension_page_id
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .canvas.navigation import PathNavigation


@dataclass
class NodePageContext:
    """传给扩展的最小能力集合，扩展只读写自己所属节点的那一页。

    read 返回独立业务数据；write 经过导图输入检查、事务和自动保存。扩展只能
    通知视口变化、报告消息和查询颜色，不能创建顶级标签或直接写导图文件。
    控件关闭后写入会被宿主拒绝；全部回调只允许在 GUI 线程同步调用。
    """

    read: Callable[[], dict]
    write: Callable[[dict], None]
    state_changed: Callable[[], None]
    report: Callable[[str], None]
    theme_color: Callable
    theme_icon: Callable
    compact: bool = False


class NodePageWidget(QWidget):
    """扩展页面的基础接口；内容、视口及编辑权限分别更新，禁止在刷新中写回。"""

    def set_data(self, data: dict) -> None:
        """回显独立数据副本，撤销、重做或其他视图修改时也会调用。"""

    def capture_state(self) -> dict:
        """返回可序列化的视口状态，不包含业务数据或文件路径。"""
        return {}

    def restore_state(self, state: dict) -> None:
        """恢复有效视口；无效或旧字段由具体页面忽略。"""

    def set_editable(self, editable: bool) -> None:
        """同步导图的只读状态；宿主写入口仍会再次校验。"""

    def apply_theme(self) -> None:
        """重新查询上下文色板，只修改页面外观。"""

    def dispose(self) -> None:
        """停止后台任务和外部订阅；QWidget 由宿主统一销毁。"""


@dataclass(frozen=True)
class NodePageProvider:
    """一种节点专用详情页的声明；每节点每类型一页，由所属导图统一托管。

    create_data 生成首次打开时的独立数据，factory 构造未显示的页面控件。
    业务版本不支持时宿主展示保留提示，绝不自动覆盖新版本数据。
    """

    id: str
    title: str
    factory: Callable[[NodePageContext, QWidget], NodePageWidget]
    create_data: Callable[[], dict]
    version: int = 1


class NodePageRegistry:
    """窗口级扩展注册表；发现失败只影响该扩展，主导图始终可用。"""

    def __init__(self, providers=()):
        self._providers: dict[str, NodePageProvider] = {}
        self.errors: list[str] = []
        for provider in providers:
            self.register(provider)

    def register(self, provider: NodePageProvider) -> None:
        """校验稳定标识、显示名称及工厂，重复标识失败且保留原注册项。"""
        from mindmap_core.node_pages import validate_extension_pages

        if not isinstance(provider, NodePageProvider):
            raise MindMapError("扩展入口必须提供 NodePageProvider")
        validate_extension_pages({provider.id: {"version": provider.version, "data": {}}})
        if (not isinstance(provider.title, str) or not provider.title.strip()
                or not callable(provider.factory) or not callable(provider.create_data)):
            raise MindMapError("扩展页面的名称或工厂无效")
        if provider.id in self._providers:
            raise MindMapError(f"扩展页面类型重复：{provider.id}")
        self._providers[provider.id] = provider

    def get(self, type_id: str) -> NodePageProvider | None:
        """查询可用的页面实现，不导入文件或修改数据。"""
        return self._providers.get(type_id)

    def providers(self) -> tuple[NodePageProvider, ...]:
        """按注册顺序返回类型快照，用于节点的扩展详情菜单。"""
        return tuple(self._providers.values())

    @classmethod
    def discover(cls) -> NodePageRegistry:
        """通过 mindmap.node_pages 入口发现节点专用详情页。"""
        registry = cls()
        for entry in sorted(entry_points(group="mindmap.node_pages"), key=lambda item: item.name):
            try:
                provider = entry.load()
                if provider.id != entry.name:
                    raise MindMapError("入口标识与页面类型不一致")
                registry.register(provider)
            except Exception as error:
                registry.errors.append(f"扩展 {entry.name} 加载失败：{error}")
        return registry


class UnavailableNodePage(NodePageWidget):
    """扩展未安装、版本不支持或构造失败时保留数据并提供返回入口。"""

    def __init__(self, message: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        label = QLabel(message + "\n页面数据仍保存在导图中，可以继续保存和编辑其他节点。", self)
        label.setWordWrap(True)
        layout.addStretch()
        layout.addWidget(label)
        layout.addStretch()


class NodePageHost(QStackedWidget):
    """一份导图标签内的页面容器；导图画布与扩展页面共享文档和历史。

    导图保持原控件及视口，返回无需重新加载。扩展只在进入时创建；离开时捕获
    视口并释放订阅。不同标签分别持有容器，内容由同一引擎同步。
    """

    page_changed = Signal()
    state_changed = Signal()

    def __init__(self, canvas, registry: NodePageRegistry, *, prepare_input, theme_color, theme_icon, report):
        super().__init__()
        self.canvas, self.registry = canvas, registry
        self._prepare_input, self._theme_color, self._report = prepare_input, theme_color, report
        self._theme_icon = theme_icon
        self._target: tuple[str, str] | None = None
        self._record = None
        self._page: NodePageWidget | None = None
        self._page_token = None
        self._states: dict[str, dict] = {}
        self._disposed = False
        self._switching = False
        self.addWidget(canvas)
        self.detail = QWidget(self)
        layout = QVBoxLayout(self.detail)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QHBoxLayout()
        self.navigation = PathNavigation(self.detail)
        self.navigation.back_requested.connect(self.close_page)
        self.navigation.navigate_requested.connect(self._navigate)
        header.addWidget(self.navigation, 1)
        self.remove_button = QToolButton(self.detail)
        self.remove_button.setText("删除此详情页…")
        self.remove_button.clicked.connect(self._confirm_remove)
        header.addWidget(self.remove_button)
        layout.addLayout(header)
        self._body = QVBoxLayout()
        layout.addLayout(self._body, 1)
        self.addWidget(self.detail)
        self.setCurrentWidget(canvas)
        # 停靠层在关闭侧栏后把焦点交给标签根控件；初始状态也要转发到导图，
        # 不能只在第一次退出扩展页后才建立焦点代理。
        self.setFocusProxy(canvas)
        self.menu_action = canvas.register_action(
            "extension_pages", "扩展详情…", lambda _: self.show_menu(), mutating=False)
        canvas.document_changed.connect(self._sync)
        canvas.selection_changed.connect(self._selection_changed)
        canvas.page_changed.connect(self._canvas_navigated)
        canvas.editable_changed.connect(self._sync_permissions)
        canvas.extension_page_requested.connect(self.open_node_pages)
        self._sync_permissions()

    @property
    def active(self) -> bool:
        """是否正在查看节点的专用详情页。"""
        return self._target is not None

    @property
    def current_page_id(self) -> str | None:
        """返回稳定的扩展导航标识；没有扩展页时沿用导图页面标识。"""
        return extension_page_id(*self._target) if self._target else self.canvas.current_page_id

    def populate_menu(self, menu: QMenu) -> None:
        """按选中节点列出已安装类型及未安装但已有数据的类型。"""
        if self.canvas.selection_count != 1:
            hint = menu.addAction("请先选择一个节点")
            hint.setEnabled(False)
            return
        node_id = self.canvas.selected_node_ids()[0]
        node = self.canvas.engine.get_node(node_id)
        types = {provider.id: provider.title for provider in self.registry.providers()}
        types.update({key: types.get(key, key) for key in node.extension_pages})
        for type_id, title in types.items():
            existing = type_id in node.extension_pages
            action = menu.addAction(("打开" if existing else "添加") + title)
            action.setEnabled(not node.is_image_node and (existing or self.canvas.editable))
            action.triggered.connect(lambda checked=False, key=type_id:
                                     self.canvas.call(lambda: self.open_page(node_id, key, create=True)))
        if not types:
            menu.addAction("暂无可用扩展").setEnabled(False)

    def show_menu(self) -> None:
        """从节点菜单弹出类型选择，菜单动作再次验证节点和编辑权限。"""
        from PySide6.QtGui import QCursor

        menu = QMenu(self)
        try:
            self.populate_menu(menu)
            menu.exec(QCursor.pos())
        finally:
            menu.deleteLater()

    def open_node_pages(self, node_id: str) -> None:
        """点击节点图标时直接打开唯一扩展；多种扩展由类型菜单选择。"""
        node = self.canvas.engine.get_node(node_id)
        if len(node.extension_pages) == 1:
            self.open_page(node_id, next(iter(node.extension_pages)))
        else:
            self.canvas.select_nodes([node_id])
            self.show_menu()

    def open_page(self, node_id: str, type_id: str, *, create: bool = False) -> bool:
        """进入已有页面或显式创建一页，返回成功状态；输入未完成时原地等待。

        创建和保存统一走引擎接口。页面扩展故障使用占位控件报告，已存内容不丢失。
        """
        if self._disposed or not self._prepare_input():
            return False
        provider = self.registry.get(type_id)
        record = self.canvas.engine.extension_page(node_id, type_id)
        if record is None:
            if not create or provider is None:
                raise MindMapError("该节点没有此扩展详情页")
            if not self.canvas.editable:
                raise MindMapError("当前导图为只读模式")
            self.canvas.engine.set_extension_page(node_id, type_id, provider.create_data(), version=provider.version)
            record = self.canvas.engine.extension_page(node_id, type_id)
        self._release_page()
        self._switching = True
        try:
            # 先定位拥有者，返回时仍使用这张导图的现有视口；普通节点选择不重置缩放。
            if (self.canvas.engine.node_page_id(node_id) != self.canvas.current_page_id
                    or node_id not in self.canvas.document_view.nodes):
                self.canvas.navigate_to(self.canvas.engine.node_page_id(node_id), node_id)
            self.canvas.select_nodes([node_id])
            self._target, self._record = (node_id, type_id), deepcopy(record)
            self._page_token = object()
            self._page = self._create_page(provider, record)
            self._body.addWidget(self._page)
            try:
                self._page.restore_state(deepcopy(self._states.get(self.current_page_id, {})))
            except Exception as error:
                # 视口故障不回滚已存内容；页面仍可用默认视口打开并由用户重新调整。
                self.canvas.error_occurred.emit(f"恢复扩展视口失败：{error}")
            self._sync_permissions()
            self.refresh_theme()
            self._sync_path()
            self.setCurrentWidget(self.detail)
            self.setFocusProxy(self._page)
            self._page.setFocus()
        finally:
            self._switching = False
        self.page_changed.emit()
        self.state_changed.emit()
        return True

    def _create_page(self, provider, record):
        if provider is None or provider.version != record["version"]:
            return UnavailableNodePage("此扩展未安装或页面版本暂不受支持。", self.detail)
        target = self._target
        token = self._page_token

        def read():
            if self._disposed or self._page_token is not token:
                raise MindMapError("此详情页已关闭")
            return self.canvas.engine.extension_page(*target)["data"]

        def write(data):
            # 回调捕获页面身份，拒绝已关闭控件的迟到写入，防止写到后来打开的节点。
            if self._disposed or self._page_token is not token or not self.canvas.editable:
                raise MindMapError("此详情页已关闭或导图为只读模式")
            current = self.canvas.engine.extension_page(*target)
            if current is None or current["version"] != provider.version:
                raise MindMapError("扩展页面已删除或业务版本发生变化，请重新打开")
            if not self._prepare_input():
                raise MindMapError("请先完成导图中的输入")
            with self.canvas.engine.event_source(self.canvas.interaction_id):
                self.canvas.engine.set_extension_page(*target, data, version=provider.version)

        context = NodePageContext(
            read, write,
            self.state_changed.emit, self._report, self._theme_color, self._theme_icon)
        page = None
        try:
            page = provider.factory(context, self.detail)
            if not isinstance(page, NodePageWidget):
                raise TypeError("扩展必须返回 NodePageWidget")
            page.set_data(deepcopy(record["data"]))
            return page
        except Exception as error:
            self._page_token = object()
            if isinstance(page, QWidget):
                try:
                    if isinstance(page, NodePageWidget):
                        page.dispose()
                except Exception:
                    # 初始化失败时仍要销毁候选控件，清理异常不能掩盖原始业务错误。
                    pass
                finally:
                    page.deleteLater()
            message = f"无法打开{provider.title}：{error}"
            self.canvas.error_occurred.emit(message)
            return UnavailableNodePage(message, self.detail)

    def close_page(self) -> None:
        """返回原导图；不提交内容事务，也不改变导图缩放和滚动位置。"""
        if not self.active:
            return
        self._release_page()
        self.setCurrentWidget(self.canvas)
        self.setFocusProxy(self.canvas)
        self.canvas.setFocus()
        self.page_changed.emit()
        self.state_changed.emit()

    def _release_page(self):
        if self._page is not None:
            self._states[self.current_page_id] = self._capture_page_state()
            page, self._page = self._page, None
            self._target, self._page_token = None, None
            try:
                page.dispose()
            except Exception as error:
                self.canvas.error_occurred.emit(f"释放扩展详情失败：{error}")
            finally:
                self._body.removeWidget(page)
                page.hide()
                page.deleteLater()
        self._target, self._record, self._page_token = None, None, None

    def _sync(self, *_):
        if self._disposed or self._switching:
            return
        if self._target:
            try:
                record = self.canvas.engine.extension_page(*self._target)
            except MindMapError:
                record = None
            if record is None:
                self.close_page()
            else:
                if record["version"] != self._record["version"]:
                    # 外部更新或撤销可能跨业务版本；重新经过工厂版本门禁，不能把
                    # 新版本数据交给已打开的旧版本控件，更不能在下一次写入时降级。
                    self.open_page(*self._target)
                    return
                if record != self._record:
                    try:
                        self._page.set_data(deepcopy(record["data"]))
                        self._record = deepcopy(record)
                    except Exception as error:
                        self.canvas.error_occurred.emit(f"刷新扩展详情失败：{error}")
                        self.close_page()
                if self.active:
                    self._sync_path()
        self._sync_permissions()

    def _sync_permissions(self, *_):
        self.menu_action.setEnabled(self.canvas.selection_count == 1)
        self.remove_button.setEnabled(self.canvas.editable)
        if self._page is not None:
            self._page.set_editable(self.canvas.editable)

    def _selection_changed(self, *_):
        if self.active and not self._switching and self._target[0] not in self.canvas.selected_node_ids():
            self.close_page()
        self._sync_permissions()

    def _canvas_navigated(self, *_):
        if not self._switching:
            self.close_page()

    def _sync_path(self):
        node_id, type_id = self._target
        provider = self.registry.get(type_id)
        path = self.canvas.engine.node_path(node_id)
        path.append(PathEntry(self.current_page_id, node_id, provider.title if provider else type_id, True))
        kinds = {entry.page_id: self.canvas.engine.page_summary(entry.page_id).kind
                 for entry in path if self.canvas.engine.page_summary(entry.page_id) is not None}
        kinds[self.current_page_id] = "extension"
        self.navigation.set_path(path, page_id=self.current_page_id, page_kinds=kinds, reference_return=True)

    def _navigate(self, page_id, node_id):
        if parse_extension_page_id(page_id):
            return
        self.close_page()
        self.canvas.call(lambda: self.canvas.navigate_to(page_id, node_id))

    def _confirm_remove(self):
        if not self.active or not self.canvas.editable:
            return
        target = self._target
        answer = QMessageBox.question(self, "删除扩展详情页", "删除此节点的专用详情页？删除后可以撤销。")
        if (answer == QMessageBox.StandardButton.Yes and self._target == target
                and not self._disposed and self.canvas.editable):
            self.canvas.call(lambda: self.canvas.engine.remove_extension_page(*target))

    def capture_state(self) -> dict:
        """保存当前页面和各扩展视口；内容只存在于导图文档中。"""
        if self._page is not None:
            self._states[self.current_page_id] = self._capture_page_state()
        return {"active": self.current_page_id if self.active else None, "pages": deepcopy(self._states)}

    def _capture_page_state(self) -> dict:
        """不让第三方页面的无效视口阻断整个导图会话保存或关闭。"""
        try:
            state = self._page.capture_state()
            if not isinstance(state, dict):
                raise ValueError("扩展视口必须为对象")
            json.dumps(state, allow_nan=False)
            return deepcopy(state)
        except Exception as error:
            self.canvas.error_occurred.emit(f"读取扩展视口失败：{error}")
            return deepcopy(self._states.get(self.current_page_id, {}))

    def restore_state(self, state) -> None:
        """恢复仍存在的扩展页；旧视图或已删除的节点自然回退导图。"""
        if not isinstance(state, dict):
            return
        pages = state.get("pages")
        self._states = {key: deepcopy(value) for key, value in pages.items()
                        if parse_extension_page_id(key) and isinstance(value, dict)} if isinstance(pages, dict) else {}
        target = parse_extension_page_id(state.get("active"))
        if target:
            try:
                if self.canvas.engine.extension_page(*target) is not None:
                    self.open_page(*target)
            except MindMapError:
                pass

    def refresh_theme(self) -> None:
        """同步页面导航和当前扩展的外观。"""
        self.navigation.set_ui_colors(self.canvas.ui_colors)
        if self._page is not None:
            self._page.apply_theme()

    def dispose(self) -> None:
        """幂等释放扩展及画布订阅，画布生命周期仍由导图视图负责。"""
        if self._disposed:
            return
        self._disposed = True
        self._release_page()
        self.canvas.document_changed.disconnect(self._sync)
        self.canvas.selection_changed.disconnect(self._selection_changed)
        self.canvas.page_changed.disconnect(self._canvas_navigated)
        self.canvas.editable_changed.disconnect(self._sync_permissions)
        self.canvas.extension_page_requested.disconnect(self.open_node_pages)
