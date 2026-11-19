"""识别 Markdown 公式并在 Qt 文档内排版可缩放的矢量图形。"""

import re
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QFontDatabase, QFontInfo, QPyTextObject, QTextCharFormat, QTextFormat
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

# 代码行由 Markdown 的块解析器识别。同一段内用相同长度的反引号配对行内代码，
# 先跳过代码和转义字符，再捕获公式。单美元符号按常用规则避开金额和分隔空白。
_FORMULA = re.compile(
    r"(?P<code>(?<!`)(?P<ticks>`+)(?!`)(?:(?!\n[ \t]*\n)[\s\S])*?(?<!`)(?P=ticks)(?!`))"
    r"|(?P<escape>\\[\\$`])"
    r"|\$\$(?P<double>(?:\\[\s\S]|[^\\$]|\$(?!\$))*?)\$\$"
    r"|\\\[(?P<bracket>[\s\S]*?)\\\]"
    r"|\\\((?P<paren>[^\n]*?)\\\)"
    r"|\$(?![\s$])(?P<dollar>(?:\\[^\n]|[^\\$\n])+?)(?<!\s)\$(?![\d$])"
)
_OBJECT_TYPE = QTextFormat.ObjectTypes.UserObject
_FORMULA_INDEX = QTextFormat.Property.UserProperty


@dataclass(frozen=True, slots=True)
class _Formula:
    marker: str
    source: str
    expression: str
    display: bool


def mark_formulas(text: str) -> tuple[str, list[_Formula]]:
    """在用于 Qt 解析的字符串中临时标记 LaTeX 公式，同时返回原始分隔符及表达式。

    识别行内的单美元符号和反斜杠圆括号，以及独立公式的双美元符号和反斜杠方括号。
    根据 Markdown 的代码行范围隔离扫描。公式源码留在原节点上，标记只用于本轮排版。
    无公式分隔符时直接返回原文和空列表。
    """
    if "$" not in text and "\\(" not in text and "\\[" not in text:
        return text, []

    from markdown_it import MarkdownIt

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    offsets = [0, *(match.end() for match in re.finditer("\n", text)), len(text)]
    formulas = []
    prefix = "\ue000"
    while prefix in text:
        prefix += "\ue000"

    def replace_formula(match):
        if match["code"] is not None or match["escape"] is not None:
            return match[0]
        display = match["double"] is not None or match["bracket"] is not None
        expression = match[match.lastgroup].strip()
        if not expression:
            return match[0]
        marker = f"{prefix}{len(formulas)}\ue001"
        formulas.append(_Formula(marker, match[0], expression, display))
        # 独占一行的显示公式形成单独段落，行内的公式继续参与正文换行。
        part = match.string
        line_start = part.rfind("\n", 0, match.start()) + 1
        line_end = part.find("\n", match.end())
        before = part[line_start:match.start()]
        after = part[match.end():None if line_end == -1 else line_end]
        if display and not before.strip() and not after.strip():
            return f"\n\n{marker}\n\n"
        return marker

    parts = []
    start = 0
    for token in MarkdownIt().parse(text):
        if token.type in ("fence", "code_block"):
            left, right = (offsets[line] for line in token.map)
            parts.extend((_FORMULA.sub(replace_formula, text[start:left]), text[left:right]))
            start = right
    parts.append(_FORMULA.sub(replace_formula, text[start:]))
    return "".join(parts), formulas


def _roman_font(font, expression: str) -> str:
    if not re.search(r"[\u3400-\u9fff]", expression):
        return "DejaVu Serif"
    # 常规公式仍使用数学字体，中文 \text{} 使用 Qt 当前可用的中文字体。
    families = QFontDatabase.families(QFontDatabase.WritingSystem.SimplifiedChinese)
    for family in (font.family(), QApplication.font().family(), *families):
        if family in families:
            return family
    return "DejaVu Serif"


@lru_cache(maxsize=256)
def _formula_svg(expression: str, size: float, family: str, color: str) -> bytes | None:
    from matplotlib import rc_context
    from matplotlib.font_manager import FontProperties
    from matplotlib.mathtext import math_to_image

    output = BytesIO()
    # MathText 在进程内解析 TeX 子集，SVG 字形固定为路径，便于画布、PDF 和 SVG 共用。
    with rc_context({
        "mathtext.rm": family,
        "mathtext.it": "DejaVu Serif:italic",
        "mathtext.fallback": "stix",
        "svg.fonttype": "path",
        "text.usetex": False,
    }):
        try:
            properties = FontProperties(family=[family], size=size, math_fontfamily="custom")
            math_to_image(f"${expression}$", output, prop=properties, format="svg", color=color)
        except (ValueError, OverflowError, RecursionError):
            return None
    return output.getvalue()


class _FormulaLayout(QPyTextObject):
    """由单个 QTextDocument 拥有，供 Qt 同步测量和绘制其中的公式。"""

    def __init__(self, document):
        super().__init__(document)
        self.renderers = []
        document.documentLayout().registerHandler(_OBJECT_TYPE, self)

    def intrinsicSize(self, document, position, fmt):
        """返回公式的场景逻辑尺寸，参数由当前 Qt 文档布局调用提供。"""
        return self.renderers[fmt.property(_FORMULA_INDEX)].viewBoxF().size()

    def drawObject(self, painter, rect, document, position, fmt):
        """将公式绘制到 QTextDocument 分配的矩形，由调用方持有画笔。"""
        self.renderers[fmt.property(_FORMULA_INDEX)].render(painter, rect)


def insert_formulas(document, formulas: list[_Formula], font, color) -> None:
    """在所属 GUI 线程将 Qt 已解析的标记替换为可测量的公式对象。

    document 由调用者持有并与 formulas 对应。渲染器随文档释放，字体大小使用场景
    逻辑像素，颜色取自节点主题。无法解析的公式恢复带分隔符的原文，仍可继续编辑。
    """
    if not formulas:
        return
    pixels = QFontInfo(font).pixelSize()
    layout = None
    for formula in formulas:
        cursor = document.find(formula.marker)
        if cursor.isNull():
            continue
        size = max(1, pixels) * (1.15 if formula.display else 1.0)
        svg = _formula_svg(formula.expression, size, _roman_font(font, formula.expression), color.name())
        renderer = QSvgRenderer(QByteArray(svg)) if svg else None
        if renderer is None or not renderer.isValid() or renderer.viewBoxF().isEmpty():
            cursor.insertText(formula.source, cursor.charFormat())
            continue
        if layout is None:
            layout = document.formula_layout = _FormulaLayout(document)
        if formula.display and cursor.block().text().strip() == formula.marker:
            block_format = cursor.blockFormat()
            block_format.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            cursor.setBlockFormat(block_format)
        fmt = cursor.charFormat()
        fmt.setObjectType(_OBJECT_TYPE)
        fmt.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignMiddle)
        fmt.setProperty(_FORMULA_INDEX, len(layout.renderers))
        renderer.setParent(layout)
        layout.renderers.append(renderer)
        cursor.insertText("\ufffc", fmt)
