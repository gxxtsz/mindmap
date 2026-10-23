"""原生文档结构映射；写入版本 3，兼容读取版本 2 的单页文档。"""

from __future__ import annotations

import base64
import binascii
import re
from copy import deepcopy
from dataclasses import asdict

from mindmap_core import (
    Asset,
    LayoutConfig,
    MindMapDocument,
    MindMapError,
    MindMapPage,
    MindMapPageView,
    Node,
    Theme,
)

SCHEMA_VERSION = 3


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
    }


def from_dict(data: dict, *, resources: dict[str, bytes] | None = None) -> MindMapDocument:
    """读取当前多页格式或版本 2 单页格式，校验全部引用后返回独立文档。"""
    try:
        if not isinstance(data, dict) or data.get("format") != "mindmap":
            raise MindMapError("不是本项目的思维导图文档")
        data = deepcopy(data)
        if type(data.get("schema_version")) is not int or data["schema_version"] not in (2, SCHEMA_VERSION):
            raise MindMapError("不支持的文档格式版本")

        allowed = {"format", "schema_version", "root_id", "nodes", "assets", "layout", "theme", "metadata"}
        if data["schema_version"] == SCHEMA_VERSION:
            allowed.add("submaps")
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
                raise MindMapError("子导图字段不完整或包含未知字段")
            if not isinstance(record["layout"], dict) or set(record["layout"]) != {
                "algorithm_id", "direction", "compact", "options"
            }:
                raise MindMapError("子导图布局配置无效")
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
        )
        document.validate()
        return document
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
