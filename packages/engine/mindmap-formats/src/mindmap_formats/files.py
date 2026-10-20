"""统一文件和内存流入口；格式只处理流，原子保存由本模块负责。"""

import os
import tempfile
from io import BytesIO
from pathlib import Path

from mindmap_core import FormatRegistry, MindMapDocument, MindMapError

from .limits import MAX_ARCHIVE_BYTES
from .registry import create_default_format_registry


def load_document(
    path: str | Path, *, registry: FormatRegistry | None = None, format_id: str | None = None, options=None
) -> MindMapDocument:
    """解析格式后返回已校验的新文档；不会替换宿主文档或建立保存标记。"""
    registry = registry if registry is not None else create_default_format_registry()
    target = Path(path)
    entry = registry.resolve(target, format_id=format_id)
    try:
        if target.stat().st_size > MAX_ARCHIVE_BYTES:
            raise MindMapError("文档大小超过读取限制")
        with target.open("rb") as stream:
            return registry.read(stream, format_id=entry.info.id, options=options)
    except MindMapError:
        raise
    except OSError as exc:
        raise MindMapError(f"读取文档失败：{exc}") from exc


def save_document(
    document: MindMapDocument,
    path: str | Path,
    *,
    registry: FormatRegistry | None = None,
    format_id: str | None = None,
    options=None,
) -> None:
    """先写目标同目录的临时文件，完整刷新后原子替换；任何格式均复用此流程。

    本函数也可导出有损格式；是否能作为主文档保存由宿主查询 can_save 决定。
    保存失败保持原文件不变，且无论成功失败都不修改引擎脏状态。
    """
    registry = registry if registry is not None else create_default_format_registry()
    document.validate()
    target = Path(path).absolute()
    entry = registry.resolve(target, format_id=format_id, writing=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            registry.write(document, stream, format_id=entry.info.id, options=options)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except MindMapError:
        raise
    except OSError as exc:
        raise MindMapError(f"保存文档失败：{exc}") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def encode_document(
    document, *, registry: FormatRegistry | None = None, format_id="json", options=None
) -> bytes:
    """向内存流导出，供剪贴板和宿主集成使用，并沿用注入的格式实现。"""
    registry = registry if registry is not None else create_default_format_registry()
    stream = BytesIO()
    registry.write(document, stream, format_id=format_id, options=options)
    return stream.getvalue()


def decode_document(
    data: bytes, *, registry: FormatRegistry | None = None, format_id="json", options=None
) -> MindMapDocument:
    """从内存流导入，使用与文件打开完全相同的格式和结构校验。"""
    registry = registry if registry is not None else create_default_format_registry()
    return registry.read(BytesIO(data), format_id=format_id, options=options)
