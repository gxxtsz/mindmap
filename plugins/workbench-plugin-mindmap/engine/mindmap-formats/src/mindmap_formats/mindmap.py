"""带图片资源的 ZIP 文档容器；始终通过流读取，不向磁盘解压。"""

import json
import re
import zipfile

from mindmap_core import MindMapError

from .json_codec import _parse_json
from .limits import MAX_ARCHIVE_BYTES
from .schema import _from_dict, to_dict


class MindmapCodec:
    """完整保存节点、布局、主题和图片资源的原生容器格式。"""

    def read(self, stream, *, options):
        """先校验容器目录和容量，再解析结构、校验资源哈希，不写入文件系统。"""
        return _read_archive(stream).document

    def read_validated(self, stream, *, options):
        """读取独占新文档并返回一次性完整校验凭据，供原生文件打开链路接管。

        与 read 使用同一解析器，所有容器、字段、引用、数值及资源哈希规则相同。
        返回前已校验全部页面；借用文档的后续代码必须只读，接管后凭据不可复用。
        本接口仅由确认标准编解码器身份的格式层调度，不替代扩展的 read 覆写。
        """
        return _read_archive(stream)

    def write(self, document, stream, *, options):
        """JSON 中只写资源引用，原始图片字节独立保存，避免重复 Base64 编码。"""
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "document.json",
                json.dumps(to_dict(document, embed_assets=False), ensure_ascii=False, allow_nan=False),
            )
            for asset in document.assets.values():
                archive.writestr(f"assets/{asset.id}", asset.data)


def _read_archive(stream):
    """共用原生读取实现，解析器内部的新对象始终先完成校验再返回凭据。"""
    with zipfile.ZipFile(stream) as archive:
        entries = archive.infolist()
        names = [item.filename for item in entries]
        if len(names) != len(set(names)):
            raise MindMapError("容器包含重复文件名")
        if sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES:
            raise MindMapError("文档解压后的大小超过读取限制")
        if "document.json" not in names:
            raise MindMapError("容器缺少 document.json")
        if any(
            name != "document.json" and not re.fullmatch(r"assets/[a-f0-9]{64}", name) for name in names
        ):
            raise MindMapError("容器包含无效资源路径")
        resources = {name[7:]: archive.read(name) for name in names if name.startswith("assets/")}
        return _from_dict(_parse_json(archive.read("document.json").decode("utf-8")),
                          resources=resources, with_validation=True)
