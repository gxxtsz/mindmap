"""场景图元与临时文本编辑器；持久化修改全部交回宿主引擎。"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QPainterPath, QTextCursor
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QGraphicsTextItem


class NodeItem(QGraphicsObject):
    def __init__(self, node, visual, theme, owner=None):
        super().__init__()
        self.owner, self.node, self.visual, self.theme = owner, node, visual, theme
        self.editing = False
        self.setZValue(5)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemIsFocusable
        )
        # 节点正文、折叠按钮、只读状态的光标由视图按命中位置统一判断。
        self.setToolTip(node.note or node.hyperlink)

    def update_content(self, node, visual, theme):
        self.prepareGeometryChange()
        self.node, self.visual, self.theme = node, visual, theme
        self.setToolTip(node.note or node.hyperlink)
        self.update()

    def boundingRect(self):
        return QRectF(-2, -2, self.visual.width + 4, self.visual.height + 4)

    def shape(self):
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, self.visual.width, self.visual.height), 7, 7)
        return path

    def paint(self, painter, option, widget=None):
        self.visual.paint(
            painter,
            self.node,
            selected=self.isSelected(),
            editing=self.editing,
            controls=self.owner is not None,
            accent=self.theme.accent,
        )

    def mouseDoubleClickEvent(self, event):
        if self.owner:
            self.owner.begin_edit(self.node.id)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class NodeEditor(QGraphicsTextItem):
    """单次编辑对应单个引擎命令；输入法预编辑期间 Enter 只交给输入法处理。"""

    def __init__(self, owner, node, visual):
        super().__init__()
        self.owner, self.node_id = owner, node.id
        self.composing = False
        self.finishing = False
        self.setZValue(20)
        self.setFont(visual.font)
        self.setDefaultTextColor(visual.text_color)
        self.setPlainText(node.text)
        self.document().setDocumentMargin(0)
        self.setTextWidth(max(100, visual.text.size().width()))
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        cursor = self.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        self.setTextCursor(cursor)

    def inputMethodEvent(self, event):
        self.composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event):
        if self.composing and event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Tab,
            Qt.Key.Key_Escape,
        ):
            # 输入法通常先消费候选确认键；仍送达图元的按键不得插入换行或结束编辑。
            # 预编辑文本的最终提交/取消只根据后续 QInputMethodEvent 更新。
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.owner.finish_edit(commit=False)
            event.accept()
        elif (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab)
            and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.owner.finish_edit(commit=True)
            event.accept()
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        # 弹出菜单只是临时借用键盘焦点，关闭后仍回到当前文字草稿。
        # 尤其导图设置菜单需要在编辑期间保留禁用状态，不能因打开菜单而自动
        # 提交正文并解锁文档设置；真正切换到其他控件时仍沿用失焦提交规则。
        if event.reason() == Qt.FocusReason.PopupFocusReason:
            return
        if not self.finishing:
            # 焦点事件返回后再移除图元，避免在 Qt 虚函数调用栈中销毁编辑器。
            QTimer.singleShot(
                0, lambda: self.owner.finish_edit(commit=True) if self.owner.editor is self else None
            )
