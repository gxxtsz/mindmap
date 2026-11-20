"""独立文档格式包；算法是否已安装不影响文档的解析与保存。"""

from .directory import (
    DirectoryUpdate,
    check_directory_version,
    directory_size,
    directory_version,
    guard_directory_version,
    load_directory,
    load_validated_directory,
    prepare_directory_update,
    save_directory,
    validate_directory_capacity,
)
from .directory_storage import DirectoryVersion, DirectoryVersionConflict, is_directory_member
from .edraw import import_edraw_xml
from .files import decode_document, encode_document, load_document, load_validated_document, save_document
from .json_codec import JsonCodec, dumps, loads
from .limits import MAX_ARCHIVE_BYTES
from .mindmap import MindmapCodec
from .outline import detect_outline_format, export_outline, import_indented_fragment, import_outline
from .processing import can_process_snapshot, supports_owned_load, supports_process_save
from .registry import OutlineCodec, create_default_format_registry
from .schema import SCHEMA_VERSION, from_dict, to_dict

__all__ = [
    "MAX_ARCHIVE_BYTES",
    "SCHEMA_VERSION",
    "DirectoryUpdate",
    "DirectoryVersion",
    "DirectoryVersionConflict",
    "JsonCodec",
    "MindmapCodec",
    "OutlineCodec",
    "can_process_snapshot",
    "check_directory_version",
    "create_default_format_registry",
    "decode_document",
    "detect_outline_format",
    "directory_size",
    "directory_version",
    "dumps",
    "encode_document",
    "export_outline",
    "from_dict",
    "guard_directory_version",
    "import_outline",
    "import_indented_fragment",
    "import_edraw_xml",
    "is_directory_member",
    "load_document",
    "load_directory",
    "load_validated_directory",
    "load_validated_document",
    "loads",
    "prepare_directory_update",
    "save_document",
    "save_directory",
    "supports_owned_load",
    "supports_process_save",
    "to_dict",
    "validate_directory_capacity",
]
