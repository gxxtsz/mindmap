"""工作台运行期间的操作摘要；只保存文字，不持有文档、差量或图片资源。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PureWindowsPath

from mindmap_core import ChangeEvent, NodeMove, Theme
from mindmap_qt_ui import UiColors, toolbar_icon
from mindmap_qt_ui.ui_style import apply_ui_palette, menu_styles, scrollbar_styles, ui_colors
from PySide6.QtCore import (
    QAbstractListModel,
    QDateTime,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QListView,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

_OPERATION_NAMES = {
    "add_child": "新增子节点", "add_sibling": "新增同级节点", "insert_parent": "插入父节点",
    "update_node": "修改节点属性", "remove_nodes": "删除节点", "move_nodes": "移动节点",
    "move_up": "调整节点顺序", "set_expanded": "调整节点折叠状态",
    "set_layout": "修改导图布局", "set_theme": "修改导图主题", "set_branch_style": "修改分支样式",
    "add_image_child": "新增图片节点", "attach_image": "关联节点图片",
    "paste_fragment": "粘贴节点", "copy_nodes": "复制节点到导图",
    "create_submap": "创建子导图", "remove_submap": "删除子导图",
    "create_detail_page": "创建详情页", "remove_detail_page": "删除详情页",
    "set_submap_favorite": "修改子导图收藏",
}
_LEVEL_NAMES = {"info": "信息", "warning": "警告", "error": "错误"}


def _single_line(value: str, limit: int = 2048) -> str:
    """把用户文字压成单行并限制长度，保证一条操作始终只占一条日志记录。"""
    text = " ".join(str(value).splitlines())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _node_title(text: str, node_id: str, *, limit: int = 2048) -> str:
    """用提交时名称标识节点；无文字节点附带短标识，limit 为名称显示预算。"""
    name = _single_line(text, limit).strip() or f"无文字节点（{node_id[:8]}）"
    return f"「{name}」"


def _describe_move(move: NodeMove, event: ChangeEvent) -> str:
    """按本次事件的实际方向描述一个移动根，不借用当前文档或改变旧日志。

    跨父级明确列出来源与去向；同父级只说明发生变化的同级序号和有效方向。
    撤销交换固定快照的两端，因此“移回”及排序、换边方向与实际重放一致。
    """
    start, end = (move.before, move.after) if event.forward else (move.after, move.before)
    title = _node_title(end.node_text, move.node_id)
    source = _node_title(start.parent_text, start.parent_id)
    target = _node_title(end.parent_text, end.parent_id)
    if start.parent_id != end.parent_id:
        verb = "移回" if event.kind == "undo" else "移到"
        return f"将{title}从{source}{verb}{target}下"
    details = []
    if start.index != end.index:
        details.append(f"由第{start.index}项调整为第{end.index}项")
    if start.direction != end.direction:
        directions = {"left": "左侧", "right": "右侧", "down": "下方"}
        details.append(f"从{directions[start.direction]}移到{directions[end.direction]}")
    return f"{title}在{target}下" + "，".join(details) if details else ""


def _text_preview(value: str, limit: int, changed_at: int) -> str:
    """在字段预算内保留实际修改附近的内容，不让长公共前缀遮住文字差异。"""
    if not value:
        return "（空内容）"
    if len(value) <= limit:
        return value
    start = max(0, min(changed_at - 32, len(value) - limit + 1))
    prefix = "…" if start else ""
    available = limit - len(prefix)
    suffix = "…" if start + available < len(value) else ""
    return prefix + value[start:start + available - len(suffix)] + suffix


def _describe_text_changes(changes: list[tuple[str, str]], *, forward: bool, limit: int) -> str:
    """从事务两端的文字快照生成修改说明，返回受单条日志长度限制的纯文本。

    撤销交换旧值与新值；批量操作最多展示三组不同修改并说明剩余组数。别名链
    同步产生的相同修改由调用方合并，避免入口、子导图和详情页重复刷出同一句。
    新旧内容均分显示预算，空串显式标注；换行和制表符显示为 ↵、⇥，保持一条
    日志一行。长内容围绕首个差异取上下文，后续改名或删除不影响本次说明。
    """
    shown = changes[:3]
    suffix = f"；另有{len(changes) - 3}组文字修改" if len(changes) > 3 else ""
    field_limit = max(8, (limit - len(suffix) - len(shown) + 1
                          - len("从「」改为「」") * len(shown)) // (2 * len(shown)))
    descriptions = []
    for before, after in shown:
        before, after = (before, after) if forward else (after, before)
        # 不能直接压平换行，否则“甲\n乙”改为“甲 乙”会看起来没有变化。
        values = [text.replace("\r\n", "↵").replace("\r", "↵").replace("\n", "↵")
                  .replace("\u2028", "↵").replace("\u2029", "↵").replace("\t", "⇥")
                  for text in (before, after)]
        changed_at = 0
        if max(map(len, values)) > field_limit:
            changed_at = next((index for index, (old, new) in enumerate(zip(*values)) if old != new),
                              min(map(len, values)))
        old, new = [_text_preview(value, field_limit, changed_at) for value in values]
        descriptions.append(f"从「{old}」改为「{new}」")
    return "；".join(descriptions) + suffix


def _describe_node_structure(event: ChangeEvent) -> list[str]:
    """从实际增删的实体快照描述分支根及其直接父节点，不查询当前文档。

    起止端由 forward 决定；新增取结果端名称，删除取起始端名称，撤销删除写成
    恢复。只列出独立分支根，附属页根及随行后代不重复列为选中对象；总数按
    真正从无到有或从有到无的节点 ID 统计，包含连带页面节点，不包含父级的
    children 更新或图片资源。最多列出三处分支及其父名，始终保留完整总数。
    缺少父级快照的旧事件维持通用摘要，不根据现有选择或已删除文档猜测父级。
    """
    if event.changes is None:
        return []
    collections = event.changes.collections
    nodes = collections.get("nodes", {})
    start = 0 if event.forward else 1
    descriptions = []
    for added, side in ((True, 1 - start), (False, start)):
        changed = {key: pair[side] for key, pair in nodes.items()
                   if pair[side] is not None and pair[1 - side] is None}
        if not changed:
            continue
        snapshots = {key: pair[side] for key, pair in nodes.items() if pair[side] is not None}
        parents = {child: key for key, node in snapshots.items() for child in node.children}
        pages = {pair[side].root_id: (pair[side], "子导图" if name == "submaps" else "详情页")
                 for name in ("submaps", "detail_pages")
                 for pair in collections.get(name, {}).values() if pair[side] is not None}
        page_owners = {page.owner_id for key, (page, _) in pages.items() if key in changed}
        # 单独增删附属页时用“入口”说明归属；连带增删时仍归入所属分支，避免重复计根。
        references = {key: (page.owner_id, "入口", kind) for key, (page, kind) in pages.items()
                      if page.owner_id not in changed and page.owner_id in snapshots}
        roots = [key for key in changed if key in references or
                 key not in pages and key in parents and parents[key] not in changed]
        if not roots:
            continue
        verb = "恢复" if added and event.kind == "undo" else "新增" if added else "删除"
        branches = []
        for key in roots[:3]:
            node = changed[key]
            parent_id, owner_role, page_kind = references.get(key, (parents.get(key), "", ""))
            parent_title = _node_title(snapshots[parent_id].text, parent_id, limit=120)
            title = _node_title(node.text, key, limit=120)
            kind = page_kind or ("分支" if key in page_owners or any(child in changed for child in node.children) else "节点")
            branches.append(f"在{owner_role}{parent_title}下{verb}{kind}{title}" if added
                            else f"从{owner_role}{parent_title}下{verb}{kind}{title}")
        if len(roots) > 3:
            branches.append(f"另有{len(roots) - 3}处{verb}")
        summary = "；".join(branches)
        if not added or len(changed) > 1 or verb == "恢复":
            summary += f"，共{verb}{len(changed)}个节点"
        descriptions.append(summary)
    return descriptions


def describe_change(event: ChangeEvent) -> str:
    """从一次已提交事件生成中文摘要，不查询引擎或保留收到的变化集。

    仅供 change、undo、redo 事件使用；事务标签作为整体操作名，撤销及重做
    保留原操作的含义。增删使用该端的父子快照，仅列出独立根并统计实际节点数。
    移动信息使用事务记录的准确对象、前后父级和位置，
    不把被挤动的兄弟或随行后代误写成移动对象；缺少该信息时维持通用摘要。
    文字修改从实体差量读取旧值和新值，按重放方向描述，合并别名同步的相同修改。
    其他普通编辑的节点名称优先取新增或删除项，删除后仍可从差量读取旧名。
    所有遍历仅涉及本次变化实体，不为日志复制整张导图。
    """
    label = _OPERATION_NAMES.get(event.operation, event.operation)
    if label.isascii():
        label = "修改导图"
    prefix = {"undo": "撤销：", "redo": "重做："}.get(event.kind, "")
    descriptions = _describe_node_structure(event)
    if event.changes is not None and event.changes.moves:
        movements = [_describe_move(move, event) for move in event.changes.moves]
        descriptions.extend(text for text in movements if text)
    nodes = event.changes.collections.get("nodes", {}) if event.changes is not None else {}
    pairs = list(nodes.values())
    text_changes = list(dict.fromkeys((old.text, new.text) for old, new in pairs
                                      if old is not None and new is not None and old.text != new.text))
    if text_changes:
        if event.operation == "update_node":
            label = "修改节点文字"
        heading = prefix + label + "："
        available = 2048 - len(heading) - sum(len(text) + 1 for text in descriptions)
        descriptions.append(_describe_text_changes(text_changes, forward=event.forward, limit=available))
    if descriptions:
        return prefix + label + "：" + "；".join(descriptions)
    if event.operation == "update_node":
        if any(old and new and old.image_width != new.image_width for old, new in pairs):
            label = "调整图片大小"
    added = [new for old, new in pairs if old is None and new is not None]
    removed = [old for old, new in pairs if new is None and old is not None]
    targets = added or removed or [new or old for old, new in pairs]
    titles = ["「" + _single_line(node.text, 60) + "」" if node.text else "图片节点"
              for node in targets[:3]]
    if titles:
        label += "：" + "、".join(titles) + (f"等 {len(targets)} 个节点" if len(targets) > 3 else "")
    return prefix + label


@dataclass(frozen=True)
class _LogEntry:
    """不可变的文字记录；界面摘要与完整复制内容分开，均不引用会话或文档。"""

    time: str
    source: str
    level: str
    message: str
    source_path: str = ""

    @property
    def plain_text(self) -> str:
        return f"{self.time} [{_LEVEL_NAMES[self.level]}] [{self.source}] {self.message}"

    @property
    def display_parts(self) -> tuple[str, str]:
        """只拆分已确定语义的摘要用于排版，不从显示文字推断操作成败或日志级别。"""
        title, _, detail = self.message.partition("：")
        if title in {"撤销", "重做"}:
            action, _, detail = detail.partition("：")
            return f"{title} · {action}", detail
        if title in {"打开导图", "新建导图", "保存导图", "另存为"}:
            return title, ""
        if title == "已导出":
            return "导出图形", PureWindowsPath(detail).name
        return title, detail

    @property
    def source_label(self) -> str:
        return self.source[:-8] if self.source.lower().endswith(".mindmap") else self.source


class _LogModel(QAbstractListModel):
    """有界列表模型，追加及淘汰均发出增量通知，让 Qt 保留未淘汰记录的选择。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.entries: list[_LogEntry] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return entry.plain_text
        if role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleDescriptionRole):
            return entry.plain_text + ("\n来源：" + entry.source_path if entry.source_path else "")
        if role == Qt.ItemDataRole.UserRole:
            return entry
        return None

    def append(self, entry: _LogEntry, limit: int):
        if len(self.entries) >= limit:
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self.entries.pop(0)
            self.endRemoveRows()
        row = len(self.entries)
        self.beginInsertRows(QModelIndex(), row, row)
        self.entries.append(entry)
        self.endInsertRows()

    def clear(self):
        self.beginResetModel()
        self.entries.clear()
        self.endResetModel()


class _LogDelegate(QStyledItemDelegate):
    """按时间、级别、操作、对象、文件标签绘制记录；窄侧栏自动减少次要列。"""

    def sizeHint(self, option, index):
        return QSize(240, 26 if self.parent()._compact else 30)

    def paint(self, painter: QPainter, option, index):
        panel = self.parent()
        colors, entry = panel._colors, index.data(Qt.ItemDataRole.UserRole)
        rect = option.rect.adjusted(2, 1, -2, -1)
        painter.save()
        try:
            painter.setClipRect(option.rect)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
            if selected or hovered or entry.level == "error":
                background = QColor(colors.selected if selected else colors.hover)
                if entry.level == "error" and not (selected or hovered):
                    background = QColor(colors.error)
                    background.setAlpha(12)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(background)
                painter.drawRoundedRect(QRectF(rect), 4, 4)
            left = rect.left() + 8
            if rect.width() >= 360:
                font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
                font.setPixelSize(11)
                painter.setFont(font)
                painter.setPen(QColor(colors.muted))
                painter.drawText(QRect(left, rect.top(), 64, rect.height()), Qt.AlignmentFlag.AlignVCenter, entry.time)
                left += 76
            # 信息使用柔和圆点，异常使用带叉圆圈和警告三角，避免所有行都重复显示“信息”。
            warning = "#d4a34f" if QColor(colors.surface).lightnessF() < 0.5 else "#ae781f"
            color = colors.error if entry.level == "error" else warning if entry.level == "warning" else colors.accent
            center_x, center_y = left + 6, rect.center().y()
            painter.setPen(QPen(QColor(color), 1.3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if entry.level == "warning":
                painter.drawPolygon(QPolygonF([
                    QPointF(center_x, center_y - 6), QPointF(center_x - 6, center_y + 5),
                    QPointF(center_x + 6, center_y + 5),
                ]))
                painter.drawLine(center_x, center_y - 2, center_x, center_y + 1)
                painter.drawPoint(center_x, center_y + 3)
            elif entry.level == "error":
                painter.drawEllipse(QRectF(center_x - 6, center_y - 6, 12, 12))
                painter.drawLine(center_x - 2, center_y - 2, center_x + 2, center_y + 2)
                painter.drawLine(center_x - 2, center_y + 2, center_x + 2, center_y - 2)
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(color))
                painter.drawEllipse(QRectF(center_x - 2.5, center_y - 2.5, 5, 5))
            left += 24
            right = rect.right() - 10
            body_font = QFont(option.font)
            body_font.setPixelSize(12)
            if rect.width() >= 640:
                badge_font = QFont(body_font)
                badge_font.setPixelSize(11)
                painter.setFont(badge_font)
                metrics = QFontMetrics(badge_font)
                width = min(168, metrics.horizontalAdvance(entry.source_label) + 18)
                badge = QRect(right - width, rect.center().y() - 10, width, 20)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(colors.surface if selected else colors.hover))
                painter.drawRoundedRect(QRectF(badge), 4, 4)
                painter.setPen(QColor(colors.muted))
                painter.drawText(badge.adjusted(8, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter,
                                 metrics.elidedText(entry.source_label, Qt.TextElideMode.ElideMiddle, width - 16))
                right = badge.left() - 20
            title, detail = entry.display_parts
            title_font = QFont(body_font)
            title_font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(title_font)
            title_width = min(156, max(48, right - left)) if detail else max(0, right - left)
            title_rect = QRect(left, rect.top(), title_width, rect.height())
            painter.setPen(QColor(color if entry.level != "info" else colors.text))
            painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter,
                             QFontMetrics(title_font).elidedText(title, Qt.TextElideMode.ElideRight, title_width))
            if detail:
                detail_rect = QRect(left + title_width + 12, rect.top(),
                                    max(0, right - left - title_width - 12), rect.height())
                painter.setFont(body_font)
                painter.setPen(QColor(colors.muted))
                painter.drawText(detail_rect, Qt.AlignmentFlag.AlignVCenter,
                                 QFontMetrics(body_font).elidedText(detail, Qt.TextElideMode.ElideRight,
                                                                   detail_rect.width()))
            if option.state & QStyle.StateFlag.State_HasFocus:
                painter.setPen(QPen(QColor(colors.accent), 1, Qt.PenStyle.DotLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(QRectF(rect.adjusted(1, 1, -1, -1)), 4, 4)
        finally:
            painter.restore()


class OperationLogPanel(QWidget):
    """全窗口共享的操作列表，所有方法均在 Qt GUI 线程调用。

    本控件只保存当前运行的最近 1000 条文字，既不写磁盘，也不借用会话对象。
    宿主通过 append_entry 提交已经确定来源的摘要；复制和清空只影响日志本身。
    copy_all_action、clear_action 可交给停靠面板作为标题栏动作，生命周期随本控件。
    """

    MAX_ENTRIES = 1000

    def __init__(self, parent: QWidget | None = None):
        """建立空日志；parent 管理 Qt 所有权，实例创建时不恢复任何历史记录。"""
        super().__init__(parent)
        self._compact = False
        self._colors = ui_colors(Theme())
        self.setObjectName("mindmap_operation_log")
        self.setAccessibleName("操作日志")
        self.list_view = QListView(self)
        self.list_view.setAccessibleName("操作日志记录")
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setMouseTracking(True)
        self.model = _LogModel(self)
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(_LogDelegate(self))
        self.setFocusProxy(self.list_view)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._stack = QStackedWidget(self)
        self.empty_label = QLabel("暂无操作记录\n编辑导图后，操作会显示在这里", self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stack.addWidget(self.empty_label)
        self._stack.addWidget(self.list_view)
        self._layout.addWidget(self._stack)
        self.copy_action = QAction("复制", self)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.copy_action.triggered.connect(self.copy_selected)
        self.select_all_action = QAction("全选", self)
        self.select_all_action.setShortcut(QKeySequence.StandardKey.SelectAll)
        self.select_all_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.select_all_action.triggered.connect(self.list_view.selectAll)
        self.copy_all_action = QAction("复制全部", self)
        self.copy_all_action.setToolTip("复制全部操作日志")
        self.copy_all_action.triggered.connect(self.copy_all)
        self.clear_action = QAction("清空", self)
        self.clear_action.setToolTip("清空本次运行的操作日志")
        self.clear_action.triggered.connect(self.clear)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        self.list_view.addActions([self.copy_action, self.select_all_action,
                                  self.copy_all_action, self.clear_action])
        self.list_view.selectionModel().selectionChanged.connect(self._sync_actions)
        self._sync_actions()
        self.set_ui_colors(self._colors)

    def append_entry(self, message: str, *, source: str = "应用", level: str = "info",
                     source_path: str = "") -> None:
        """追加一条带本机时间的日志，返回 None，不移动焦点或改变面板显隐。

        message 为操作摘要，最多保留 2048 个字符；source 为记录当时固定的文件名
        或“应用”，不保存会话引用。level 接受 info、warning、error，非法值抛出
        ValueError。source_path 是可选完整路径，仅用于悬停详情，不引用会话对象。
        换行转为空格，超过 1000 条时增量删除最旧记录。
        已在底部时跟随新内容；浏览历史时保留原可见行，容量淘汰不造成跳到底部。
        """
        if level not in _LEVEL_NAMES:
            raise ValueError("日志级别必须为 info、warning 或 error")
        following, anchor, offset = self._scroll_anchor()
        stamp = QDateTime.currentDateTime().toString("HH:mm:ss")
        self.model.append(_LogEntry(stamp, _single_line(source, 256), level,
                                    _single_line(message), _single_line(source_path)), self.MAX_ENTRIES)
        self._sync_actions()
        self._restore_scroll(following, anchor, offset)

    def plain_text(self) -> str:
        """返回当前全部记录的完整纯文本，保留时间、级别、文件名及原始摘要。

        界面省略的路径及长内容仍包含在复制文本中；本方法只读取文字，不改变选择、
        滚动、剪贴板或导图，也不创建文件。空列表返回空字符串。
        """
        return "\n".join(entry.plain_text for entry in self.model.entries)

    def copy_selected(self) -> None:
        """按发生顺序复制所选记录的完整纯文本，支持 Ctrl／Shift 多选及 Ctrl+C。

        选区为空时不修改剪贴板；复制不改变行选择、滚动位置或当前导图。
        """
        indexes = sorted(self.list_view.selectionModel().selectedIndexes(), key=lambda index: index.row())
        if indexes:
            QApplication.clipboard().setText("\n".join(self.model.entries[index.row()].plain_text for index in indexes))

    def clear(self) -> None:
        """清空本次运行的记录并恢复空列表提示，返回 None，不影响导图或剪贴板。"""
        self.model.clear()
        self._sync_actions()

    def copy_all(self) -> None:
        """复制当前保留的全部日志，返回 None；不改变选区、滚动位置或导图内容。

        空日志不覆盖系统剪贴板；非空时仅提交纯文本，不导出文件或保存额外副本。
        """
        if self.model.entries:
            QApplication.clipboard().setText(self.plain_text())

    def set_ui_colors(self, colors: UiColors, *, compact: bool = False) -> None:
        """同步宿主色板和紧凑间距，返回 None，不重建内容、选区或撤销历史。

        colors 必须为宿主提供的界面色板；样式只应用于本面板及其菜单，不修改
        QApplication 或其他组件。紧凑模式的停靠标题尺寸仍由现有框架管理。
        """
        following, anchor, offset = self._scroll_anchor()
        self._colors, self._compact = colors, compact
        apply_ui_palette(self, colors)
        apply_ui_palette(self.list_view, colors)
        self.setStyleSheet(
            f"QListView {{ background:{colors.surface}; border:none; outline:none; }}"
            f"QLabel {{ color:{colors.muted}; font-size:12px; }}"
            + scrollbar_styles(colors) + menu_styles(colors)
        )
        for action, icon in ((self.copy_all_action, "copy"), (self.clear_action, "clear")):
            action.setIcon(toolbar_icon(icon, colors.muted, colors.border, colors.accent))
        self._layout.setContentsMargins(4 if compact else 6, 3 if compact else 5,
                                       4 if compact else 6, 3 if compact else 5)
        self._restore_scroll(following, anchor, offset)
        self.list_view.viewport().update()

    def _scroll_anchor(self):
        """记录顶部记录及像素偏移；模型淘汰更早记录后，持久索引仍指向原来的记录。"""
        scroll = self.list_view.verticalScrollBar()
        index = self.list_view.indexAt(QPoint(0, 0))
        return scroll.value() >= scroll.maximum(), QPersistentModelIndex(index), self.list_view.visualRect(index).top()

    def _restore_scroll(self, following, anchor, offset):
        """同步行高与范围后恢复阅读位置，尾随模式才滚动到底部；不请求焦点。"""
        self.list_view.doItemsLayout()
        if following:
            self.list_view.scrollToBottom()
        elif anchor.isValid():
            self.list_view.scrollTo(QModelIndex(anchor), QAbstractItemView.ScrollHint.PositionAtTop)
            scroll = self.list_view.verticalScrollBar()
            scroll.setValue(scroll.value() - offset)

    def _sync_actions(self):
        """同步空态与动作；标题栏图标及右键操作共用可用状态。"""
        available = bool(self.model.entries)
        for action in (self.select_all_action, self.copy_all_action, self.clear_action):
            action.setEnabled(available)
        self.copy_action.setEnabled(bool(self.list_view.selectionModel().selectedIndexes()))
        self._stack.setCurrentIndex(1 if available else 0)
