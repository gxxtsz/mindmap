"""独立 JSON 文本及二进制流编解码；剪贴板和文件复用相同解析入口。"""

import json

from mindmap_core import MindMapDocument, MindMapError

from .limits import MAX_ARCHIVE_BYTES
from .schema import _from_dict, to_dict


def dumps(document: MindMapDocument) -> str:
    """导出包含图片的独立 UTF-8 JSON 文本，保留中文字符。"""
    return json.dumps(to_dict(document), ensure_ascii=False, indent=2, allow_nan=False)


def _parse_json(text: str) -> dict:
    def unique_pairs(pairs):
        # 正常对象由 C 层一次构造，长度变化才沿错误路径定位重复键；保留原错误语义。
        result = dict(pairs)
        if len(result) != len(pairs):
            seen = set()
            for key, _ in pairs:
                if key in seen:
                    raise MindMapError(f"JSON 字段重复：{key}")
                seen.add(key)
        return result

    try:
        return json.loads(text, object_pairs_hook=unique_pairs)
    except (ValueError, RecursionError) as exc:
        raise MindMapError(f"JSON 内容损坏：{exc}") from exc


def loads(text: str) -> MindMapDocument:
    """从独立 JSON 文本创建经过完整校验的文档。"""
    return _from_dict(_parse_json(text))


class JsonCodec:
    """无损 JSON 流编解码器；拒绝重复字段、非当前版本和过大的输入。"""

    def read(self, stream, *, options):
        """解析 UTF-8 文本，允许文件开头的 UTF-8 BOM。"""
        content = stream.read(MAX_ARCHIVE_BYTES + 1)
        if len(content) > MAX_ARCHIVE_BYTES:
            raise MindMapError("文档大小超过读取限制")
        return loads(content.decode("utf-8-sig"))

    def write(self, document, stream, *, options):
        """向调用方管理的二进制流写入完整文档。"""
        stream.write(dumps(document).encode("utf-8"))
