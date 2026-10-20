"""独立文档格式包；算法是否已安装不影响文档的解析与保存。"""

from .files import decode_document, encode_document, load_document, save_document
from .json_codec import JsonCodec, dumps, loads
from .mindmap import MindmapCodec
from .outline import export_outline, import_outline
from .registry import OutlineCodec, create_default_format_registry
from .schema import SCHEMA_VERSION, from_dict, to_dict

__all__ = [
    "SCHEMA_VERSION",
    "JsonCodec",
    "MindmapCodec",
    "OutlineCodec",
    "create_default_format_registry",
    "decode_document",
    "dumps",
    "encode_document",
    "export_outline",
    "from_dict",
    "import_outline",
    "load_document",
    "loads",
    "save_document",
    "to_dict",
]
