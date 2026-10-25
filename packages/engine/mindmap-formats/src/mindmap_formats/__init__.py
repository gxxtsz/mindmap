"""独立文档格式包；算法是否已安装不影响文档的解析与保存。"""

from .edraw import import_edraw_xml
from .files import decode_document, encode_document, load_document, save_document
from .json_codec import JsonCodec, dumps, loads
from .limits import MAX_ARCHIVE_BYTES
from .mindmap import MindmapCodec
from .outline import detect_outline_format, export_outline, import_indented_fragment, import_outline
from .processing import can_process_snapshot, supports_process_save
from .registry import OutlineCodec, create_default_format_registry
from .schema import SCHEMA_VERSION, from_dict, to_dict

__all__ = [
    "MAX_ARCHIVE_BYTES",
    "SCHEMA_VERSION",
    "JsonCodec",
    "MindmapCodec",
    "OutlineCodec",
    "can_process_snapshot",
    "create_default_format_registry",
    "decode_document",
    "detect_outline_format",
    "dumps",
    "encode_document",
    "export_outline",
    "from_dict",
    "import_outline",
    "import_indented_fragment",
    "import_edraw_xml",
    "load_document",
    "loads",
    "save_document",
    "supports_process_save",
    "to_dict",
]
