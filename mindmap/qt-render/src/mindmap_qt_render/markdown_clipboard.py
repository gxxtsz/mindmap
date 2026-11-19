"""把网页剪贴板中的结构转换为节点使用的 Markdown 源码。"""

import re

from bs4 import BeautifulSoup
from markdownify import ATX, MarkdownConverter

_LANGUAGE = re.compile(r"(?:language-|lang-)([\w.+#-]+)\Z")
_RICH_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote", "math")


def _language(element) -> str:
    candidates = (element, element.find("code"))
    for tag in candidates:
        if tag is None:
            continue
        language = tag.get("data-language", "")
        if re.fullmatch(r"[\w.+#-]+", language):
            return language
        for name in tag.get("class", ()):
            if match := _LANGUAGE.fullmatch(name):
                return match[1]
    return ""


def has_rendered_structure(soup: BeautifulSoup) -> bool:
    """判断 HTML 是否描述了实际排版的代码、公式、表格等结构。

    编辑器复制的一整段 Markdown 源文件可能也套有 pre。仅凭孤立的 pre 标签无法
    区分来源，所以将无语言的单个文本块留给调用者比较纯文本。指定 Markdown 语言的
    单个源文件也走该比较。混合网页正文和代码时以网页结构为准。
    """
    if soup.find(_RICH_TAGS) or soup.select_one(".katex, .katex-display"):
        return True
    pre = soup.find("pre")
    if pre is None:
        return soup.find("code") is not None
    if soup.get_text().split() != pre.get_text().split():
        return True
    language = _language(pre).lower()
    if language in ("markdown", "md"):
        return False
    return bool(language or pre.find("code"))


def _annotated_math(element) -> str:
    annotation = element.find("annotation", attrs={"encoding": "application/x-tex"})
    return annotation.get_text().strip() if annotation is not None else ""


class _ClipboardMarkdown(MarkdownConverter):
    """保留浏览器代码块的原始缩进和 KaTeX 公式的 TeX 注解。"""

    def convert_pre(self, el, text, parent_tags):
        code = el.find("code") or el
        source = code.get_text().rstrip("\r\n")
        fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", source)), default=0))
        return f"\n\n{fence}{_language(el)}\n{source}\n{fence}\n\n"

    def convert_span(self, el, text, parent_tags):
        classes = el.get("class", ())
        if "katex-display" in classes and (tex := _annotated_math(el)):
            return f"\n\n$$\n{tex}\n$$\n\n"
        if "katex" in classes and (tex := _annotated_math(el)):
            if el.find_parent(class_="katex-display") is not None:
                return f"\n\n$$\n{tex}\n$$\n\n"
            return f"\\({tex}\\)"
        return text

    def convert_math(self, el, text, parent_tags):
        tex = _annotated_math(el)
        if not tex:
            return text
        if el.get("display") == "block":
            return f"\n\n$$\n{tex}\n$$\n\n"
        return f"\\({tex}\\)"


def clipboard_html_to_markdown(soup: BeautifulSoup) -> str:
    """将已解析的剪贴板片段转换为可编辑正文，返回独立字符串。

    保留段落、表格、行内代码和带语言的围栏代码。公式使用原始 TeX 注解，避免
    拼接网页用于视觉排版和辅助技术的重复文本。只在借用期间读取和整理 HTML 树，
    所有内容处理都在进程内完成。
    """
    for element in soup(["script", "style"]):
        element.decompose()
    for element in soup.select("pre br"):
        element.replace_with("\n")
    return _ClipboardMarkdown(heading_style=ATX).convert_soup(soup).strip("\n")
