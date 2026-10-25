"""原生文档结构映射；写入版本 5，兼容版本 2、3、4 文档。"""

from __future__ import annotations

import base64
import binascii
import re
from copy import deepcopy
from dataclasses import asdict

from mindmap_core import (
    Asset,
    InspirationArea,
    LayoutConfig,
    MindMapDocument,
    MindMapError,
    MindMapPage,
    MindMapPageView,
    Node,
    Theme,
    ValidatedDocument,
)

SCHEMA_VERSION = 5


def to_dict(document: MindMapDocument, *, embed_assets: bool = True) -> dict:
    """转换为可独立交换的 JSON 对象。

    节点树使用 root_id + nodes 的邻接表表达，避免深层嵌套超出 JSON 递归限制。
    默认内嵌资源；容器写入时由调用方关闭内嵌并将原始字节写入 assets/。
    """
    if isinstance(document, MindMapPageView):
        raise MindMapError("单页读模型不能保存为完整文件，请使用引擎的完整文档快照")
    document.validate()
    assets = []
    for asset in document.assets.values():
        record = {"id": asset.id, "media_type": asset.media_type}
        if embed_assets:
            record["data"] = base64.b64encode(asset.data).decode("ascii")
        else:
            record["path"] = f"assets/{asset.id}"
        assets.append(record)
    return {
        "format": "mindmap",
        "schema_version": SCHEMA_VERSION,
        "root_id": document.root_id,
        "nodes": [asdict(node) for node in document.nodes.values()],
        "assets": assets,
        "layout": asdict(document.layout),
        "theme": asdict(document.theme),
        "metadata": deepcopy(document.metadata),
        "submaps": [asdict(page) for page in document.submaps.values()],
        "detail_pages": [asdict(page) for page in document.detail_pages.values()],
        "inspiration": asdict(document.inspiration),
    }


def from_dict(data: dict, *, resources: dict[str, bytes] | None = None) -> MindMapDocument:
    """读取当前多页格式或版本 2 单页格式，校验全部引用后返回独立文档。"""
    return _from_dict(data, resources=resources, copy_input=True)


def _from_dict(data: dict, *, resources=None, copy_input=False,
               with_validation=False) -> MindMapDocument | ValidatedDocument:
    """解析内部持有的 JSON 对象，避免对刚解码的整份邻接表再次深拷贝。

    只有本包 JSON 解码器可以省略复制：解析对象未向外暴露，节点中的列表、样式和
    元数据可直接移交给返回文档。公共 from_dict 始终传入 copy_input=True，保证调用方
    后续修改原始字典不会影响文档。容量、字段、树结构及资源哈希校验保持一致。
    with_validation 仅供原生独占加载返回一次性凭据；两种返回方式均完整校验一次。
    """
    try:
        if not isinstance(data, dict) or data.get("format") != "mindmap":
            raise MindMapError("不是本项目的思维导图文档")
        if copy_input:
            data = deepcopy(data)
        if type(data.get("schema_version")) is not int or data["schema_version"] not in (2, 3, 4, SCHEMA_VERSION):
            raise MindMapError("不支持的文档格式版本")

        allowed = {"format", "schema_version", "root_id", "nodes", "assets", "layout", "theme", "metadata"}
        if data["schema_version"] >= 3:
            allowed.add("submaps")
        if data["schema_version"] >= 4:
            allowed.add("detail_pages")
        if data["schema_version"] >= 5:
            allowed.add("inspiration")
        if data.keys() - allowed:
            raise MindMapError("文档包含未知字段，请使用当前文件格式")
        if "layout" not in data or not isinstance(data["layout"], dict):
            raise MindMapError("文档缺少有效布局配置")
        if set(data["layout"]) != {"algorithm_id", "direction", "compact", "options"}:
            raise MindMapError("布局配置字段不完整或包含未知字段")

        def entities(name, factory):
            records = data.get(name, [])
            if not isinstance(records, list):
                raise MindMapError(f"{name} 必须为列表")
            result = {}
            for record in records:
                if not isinstance(record, dict) or "id" not in record:
                    raise MindMapError(f"{name} 中缺少显式 ID")
                entity = factory(**record)
                if entity.id in result:
                    raise MindMapError(f"重复标识：{entity.id}")
                result[entity.id] = entity
            return result

        assets = {}
        asset_records = data.get("assets", [])
        if not isinstance(asset_records, list):
            raise MindMapError("assets 必须为列表")
        for record in asset_records:
            if not isinstance(record, dict):
                raise MindMapError("资源条目必须为对象")
            key = record["id"]
            if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key) or key in assets:
                raise MindMapError("资源标识无效或重复")
            if "data" in record:
                content = base64.b64decode(record["data"], validate=True)
            elif resources is not None and record.get("path") == f"assets/{key}":
                if key not in resources:
                    raise MindMapError(f"图片资源缺失：{key}")
                content = resources[key]
            else:
                raise MindMapError(f"图片资源缺失：{key}")
            assets[key] = Asset(key, content, record["media_type"])
        def page_factory(**record):
            if set(record) != {"id", "root_id", "owner_id", "layout", "theme"}:
                raise MindMapError("页面字段不完整或包含未知字段")
            if not isinstance(record["layout"], dict) or set(record["layout"]) != {
                "algorithm_id", "direction", "compact", "options"
            }:
                raise MindMapError("页面布局配置无效")
            return MindMapPage(record["id"], record["root_id"], record["owner_id"],
                               LayoutConfig(**record["layout"]), Theme(**record["theme"]))

        document = MindMapDocument(
            root_id=data["root_id"],
            nodes=entities("nodes", Node),
            assets=assets,
            layout=LayoutConfig(**data["layout"]),
            theme=Theme(**data.get("theme", {})),
            metadata=data.get("metadata", {}),
            submaps=entities("submaps", page_factory),
            detail_pages=entities("detail_pages", page_factory),
        )
        if "inspiration" in data:
            record = data["inspiration"]
            if not isinstance(record, dict) or set(record) != {"id", "root_ids", "positions", "layout", "theme"}:
                raise MindMapError("灵感区字段不完整或包含未知字段")
            if not isinstance(record["positions"], dict):
                raise MindMapError("灵感区坐标必须为对象")
            positions = {key: tuple(value) if isinstance(value, list) else value
                         for key, value in record["positions"].items()}
            document.inspiration = InspirationArea(record["id"], record["root_ids"], positions,
                                                   LayoutConfig(**record["layout"]), Theme(**record["theme"]))
        else:
            document.inspiration = InspirationArea(layout=deepcopy(document.layout), theme=deepcopy(document.theme))
        validation = ValidatedDocument(document)
        return validation if with_validation else validation.document
    except MindMapError:
        raise
    except (
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
        OverflowError,
        RecursionError,
        binascii.Error,
    ) as exc:
        raise MindMapError(f"文档结构或字段无效：{exc}") from exc
