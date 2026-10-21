"""交互节点与临时文本编辑器；静态绘制复用渲染包，修改交回宿主引擎。"""

from __future__ import annotations

from mindmap_qt_render.items import NodeItem as StaticNodeItem
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QGraphicsTextItem


class NodeItem(StaticNodeItem):
    """保持原节点构造接口；有宿主时转发双击编辑，导出图元无需创建该交互对象。"""

    def __init__(self, node, visual, theme, owner=None):
        super().__init__(node, visual, theme, controls=owner is not None)
        self.owner = owner

    def paint(self, painter, option, widget=None):
        """保留原 owner 切换语义；展示仍委托静态图元，不在绘制期间修改文档。"""
        self.controls = self.owner is not None
        super().paint(painter, option, widget)

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
