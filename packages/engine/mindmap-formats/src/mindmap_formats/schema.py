"""当前版本的文档结构映射；不依赖任何布局实现，不提供旧数据迁移。"""

from __future__ import annotations

import base64
import binascii
import re
from copy import deepcopy
from dataclasses import asdict

from mindmap_core import Asset, LayoutConfig, MindMapDocument, MindMapError, Node, Theme

SCHEMA_VERSION = 2


def to_dict(document: MindMapDocument, *, embed_assets: bool = True) -> dict:
    """转换为可独立交换的 JSON 对象。

    节点树使用 root_id + nodes 的邻接表表达，避免深层嵌套超出 JSON 递归限制。
    默认内嵌资源；容器写入时由调用方关闭内嵌并将原始字节写入 assets/。
    """
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
    }


def from_dict(data: dict, *, resources: dict[str, bytes] | None = None) -> MindMapDocument:
    """只解析当前版本，完成树、字段、布局配置和资源校验后返回独立文档。"""
    try:
        if not isinstance(data, dict) or data.get("format") != "mindmap":
            raise MindMapError("不是本项目的思维导图文档")
        data = deepcopy(data)
        if type(data.get("schema_version")) is not int or data["schema_version"] != SCHEMA_VERSION:
            raise MindMapError("不支持的文档格式版本")

        allowed = {"format", "schema_version", "root_id", "nodes", "assets", "layout", "theme", "metadata"}
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
        document = MindMapDocument(
            root_id=data["root_id"],
            nodes=entities("nodes", Node),
            assets=assets,
            layout=LayoutConfig(**data["layout"]),
            theme=Theme(**data.get("theme", {})),
            metadata=data.get("metadata", {}),
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
