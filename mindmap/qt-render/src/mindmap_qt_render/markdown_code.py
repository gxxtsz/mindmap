"""为 Qt 解析出的 Markdown 代码设置等宽字体、背景和语法颜色。"""

from functools import lru_cache
from itertools import accumulate

from PySide6.QtGui import QColor, QFont, QFontDatabase, QTextCharFormat, QTextCursor, QTextFormat, QTextTable

_CODE_LANGUAGE = QTextFormat.Property.BlockCodeLanguage


@lru_cache(maxsize=32)
def _lexer(language):
    from pygments.lexers import get_lexer_by_name
    from pygments.util import ClassNotFound

    try:
        return get_lexer_by_name(language)
    except ClassNotFound:
        return None


def _highlight(cursor, source, start, language, light):
    lexer = _lexer(language) if language else None
    if lexer is None:
        return

    from pygments.styles import get_style_by_name

    style = get_style_by_name("default" if light else "one-dark")
    offsets = list(accumulate((2 if ord(char) > 0xFFFF else 1 for char in source), initial=0))
    for index, token, text in lexer.get_tokens_unprocessed(source):
        color = style.style_for_token(token)["color"]
        if color and text:
            cursor.setPosition(start + offsets[index])
            cursor.setPosition(start + offsets[index + len(text)], QTextCursor.MoveMode.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(f"#{color}"))
            cursor.mergeCharFormat(fmt)


def _fix_table_after_code(document) -> None:
    # Qt 可能将围栏后的一次段落切换插在表格的首个表头单元格内。
    for frame in document.rootFrame().childFrames():
        if not isinstance(frame, QTextTable):
            continue
        cell = frame.cellAt(0, 0)
        cursor = cell.firstCursorPosition()
        first = cursor.block()
        following = first.next()
        if (first.text() or not first.previous().blockFormat().hasProperty(_CODE_LANGUAGE)
                or not following.isValid() or following.position() > cell.lastCursorPosition().position()):
            continue
        cursor.deleteChar()
        cursor.setPosition(cell.lastCursorPosition().position(), QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Bold)
        cursor.mergeCharFormat(fmt)


def style_code(document, background: QColor, foreground: QColor) -> None:
    """在 Qt GUI 线程装饰 Markdown 已识别的代码，并保留原有代码块结构。

    document 为尚未发布的排版实例，背景和前景来自节点配色。围栏代码允许折行，
    可识别的语言应用语法颜色。行内代码使用等宽字体和底色。只更新本次排版，源码
    和正式节点由调用方管理。未知语言沿用等宽样式。
    """
    _fix_table_after_code(document)
    fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    mono = QTextCharFormat()
    mono.setFontFamilies([fixed.family()])
    mono.setFontStyleHint(QFont.StyleHint.Monospace)
    mono.setFontFixedPitch(True)
    light = foreground.lightnessF() < 0.5
    shade = background.darker(106) if light else background.lighter(122)
    inline = QTextCharFormat(mono)
    inline.setBackground(shade)
    cursor = QTextCursor(document)
    cursor.beginEditBlock()
    try:
        block = document.firstBlock()
        while block.isValid():
            if block.blockFormat().hasProperty(_CODE_LANGUAGE):
                language = block.blockFormat().stringProperty(_CODE_LANGUAGE)
                start, lines = block.position(), []
                while block.isValid() and block.blockFormat().hasProperty(_CODE_LANGUAGE):
                    fmt = block.blockFormat()
                    if fmt.stringProperty(_CODE_LANGUAGE) != language:
                        break
                    fmt.setNonBreakableLines(False)
                    fmt.setBackground(shade)
                    fmt.setLeftMargin(6)
                    fmt.setRightMargin(6)
                    cursor.setPosition(block.position())
                    cursor.setBlockFormat(fmt)
                    cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
                    if cursor.hasSelection():
                        cursor.mergeCharFormat(mono)
                    lines.append(block.text())
                    block = block.next()
                _highlight(cursor, "\n".join(lines), start, language, light)
                continue
            spans = []
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.charFormat().fontFixedPitch():
                    spans.append((fragment.position(), fragment.length()))
                iterator += 1
            for start, length in spans:
                cursor.setPosition(start)
                cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(inline)
            block = block.next()
    finally:
        cursor.endEditBlock()
