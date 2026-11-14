"""原生导图目录编解码；按页保存原始实体，读取后统一校验跨页关系。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from mindmap_core import MindMapDocument, MindMapError, ValidatedDocument
from mindmap_core.node_pages import GRAPH_PAGE_TYPE, graph_members

from .directory_storage import (
    MANIFEST,
    DirectoryVersion,
    DirectoryVersionConflict,
    commit_directory,
    directory_lock,
    disk_version,
    managed_files,
    recover_directory,
    require_version,
)
from .json_codec import _parse_json
from .limits import MAX_ARCHIVE_BYTES
from .schema import SCHEMA_VERSION, _from_dict, to_dict

_DIRECTORY_FORMAT = "mindmap-directory"
_DIRECTORY_VERSION = 1
_PAGE_GROUPS = ("submaps", "detail_pages")
_FIXED_PAGES = ("pages/main.json", "pages/inspiration.json")


def _page_file(page_id: str) -> str:
    # 现有 UUID 可直接用于文件名。其他合法 ID 仍保持原值，并使用摘要作为文件名，
    # 避免路径分隔符、Windows 保留名称、大小写碰撞及文件名长度上限。
    key = (page_id if re.fullmatch(r"[a-f0-9]{32}", page_id)
           else hashlib.sha256(page_id.encode("utf-8", errors="surrogatepass")).hexdigest())
    return f"pages/{key}.json"


def _page_nodes(document: MindMapDocument, roots: list[str]) -> list[str]:
    """从原始实体收集本页节点及图内文档节点，附属树页由各自的根单独收集。"""
    stack = roots.copy()
    keys = []
    while stack:
        node_id = stack.pop()
        node = document.nodes[node_id]
        keys.append(node_id)
        stack.extend(node.children)
        stack.extend(graph_members(node.extension_pages.get(GRAPH_PAGE_TYPE)))
    return sorted(keys)


def _json_bytes(record: dict) -> bytes:
    return (json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _preserve_checkout_line_endings(
    payload: dict[str, bytes], current: DirectoryVersion, version: DirectoryVersion,
) -> DirectoryVersion:
    """按当前内容选择待提交的文件版本，保留 Git 检出的纯换行差异。"""
    before = {name: (size, digest) for name, size, digest in current.files}
    entries = []
    for name, size, digest in version.files:
        old = before.get(name)
        if old is not None and old[0] > size and name.endswith(".json"):
            content = payload[name]
            if old[0] == size + content.count(b"\n"):
                crlf_digest = hashlib.sha256(content.replace(b"\n", b"\r\n")).hexdigest()
                if old[1] == crlf_digest:
                    size, digest = old
        entries.append((name, size, digest))
    return DirectoryVersion(tuple(entries))


def _encode(document: MindMapDocument) -> dict[str, bytes]:
    data = to_dict(document, embed_assets=False)
    data.update(format=_DIRECTORY_FORMAT, schema_version=_DIRECTORY_VERSION)
    nodes = {record["id"]: record for record in data.pop("nodes")}
    inspiration = data.pop("inspiration")
    pages = {
        _FIXED_PAGES[0]: (None, [document.root_id]),
        _FIXED_PAGES[1]: (inspiration, inspiration["root_ids"]),
    }
    for group in _PAGE_GROUPS:
        records = data.pop(group)
        data[group] = [record["id"] for record in records]
        for record in records:
            pages[_page_file(record["id"])] = (record, [record["root_id"]])
    data["assets"].sort(key=lambda record: record["id"])
    payload = {MANIFEST: _json_bytes(data)}
    for name, (page, roots) in pages.items():
        record = {"page": page} if page is not None else {}
        record["nodes"] = [nodes[key] for key in _page_nodes(document, roots)]
        payload[name] = _json_bytes(record)
    payload.update((f"assets/{key}", asset.data) for key, asset in document.assets.items())
    return payload


def _decode(root: Path) -> tuple[ValidatedDocument, DirectoryVersion]:
    sizes = managed_files(root)
    if sizes is None or MANIFEST not in sizes:
        raise MindMapError(f"导图目录缺少 {MANIFEST}：{root}")
    if sum(sizes.values()) > MAX_ARCHIVE_BYTES:
        raise MindMapError("导图目录大小超过读取限制")
    entries = {}

    def read(name: str) -> bytes:
        if name not in sizes or name in entries:
            raise MindMapError(f"导图文件缺失或重复引用：{name}")
        size = sizes[name]
        with (root / name).open("rb") as stream:
            content = stream.read(size + 1)
        if len(content) != size:
            raise DirectoryVersionConflict("读取期间导图目录已被外部修改")
        entries[name] = (name, size, hashlib.sha256(content).hexdigest())
        return content

    def read_object(name: str) -> dict:
        record = _parse_json(read(name).decode("utf-8-sig"))
        if not isinstance(record, dict):
            raise MindMapError(f"导图文件必须是 JSON 对象：{name}")
        return record

    manifest = read_object(MANIFEST)
    if (manifest.keys() != {"format", "schema_version", "root_id", "layout", "theme", "metadata",
                            "assets", *_PAGE_GROUPS}
            or manifest["format"] != _DIRECTORY_FORMAT or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != _DIRECTORY_VERSION):
        raise MindMapError("导图目录清单的格式或版本无效")
    nodes = []
    membership = {}

    def read_page(name: str, *, has_page_record: bool) -> dict:
        record = read_object(name)
        expected_keys = {"page", "nodes"} if has_page_record else {"nodes"}
        if (record.keys() != expected_keys or not isinstance(record["nodes"], list)
                or has_page_record and not isinstance(record["page"], dict)):
            raise MindMapError(f"导图页面结构无效：{name}")
        members = record.pop("nodes")
        if not all(isinstance(node, dict) and isinstance(node.get("id"), str) for node in members):
            raise MindMapError(f"导图页面包含无效节点：{name}")
        nodes.extend(members)
        membership[name] = sorted(node["id"] for node in members)
        return record

    read_page(_FIXED_PAGES[0], has_page_record=False)
    inspiration = read_page(_FIXED_PAGES[1], has_page_record=True)["page"]
    for group in _PAGE_GROUPS:
        page_ids = manifest[group]
        if not isinstance(page_ids, list) or not all(isinstance(key, str) and key for key in page_ids):
            raise MindMapError("导图清单包含无效页面标识")
        records = []
        for key in page_ids:
            page = read_page(_page_file(key), has_page_record=True)["page"]
            if page.get("id") != key:
                raise MindMapError(f"清单与页面标识不符：{key}")
            records.append(page)
        manifest[group] = records
    resources = {}
    if not isinstance(manifest["assets"], list):
        raise MindMapError("导图图片清单必须是列表")
    for asset in manifest["assets"]:
        if not isinstance(asset, dict) or asset.keys() != {"id", "path", "media_type"}:
            raise MindMapError("导图图片清单条目无效")
        key = asset["id"]
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key) or asset["path"] != f"assets/{key}":
            raise MindMapError("导图图片标识或路径无效")
        resources[key] = read(asset["path"])
    if entries.keys() != sizes.keys():
        raise MindMapError("导图目录包含未列入清单的页面或图片")
    manifest.update(format="mindmap", schema_version=SCHEMA_VERSION, nodes=nodes, inspiration=inspiration)
    validation = _from_dict(manifest, resources=resources, with_validation=True)
    document = validation.document
    roots = {
        _FIXED_PAGES[0]: [document.root_id],
        _FIXED_PAGES[1]: document.inspiration.root_ids,
        **{_page_file(page.id): [page.root_id] for page in document.iter_pages()},
    }
    for name, keys in membership.items():
        if keys != _page_nodes(document, roots[name]):
            raise MindMapError(f"导图节点归属与页面内容不符：{name}")
    return validation, DirectoryVersion(tuple(entries[name] for name in sizes))


def directory_size(path: str | Path) -> int | None:
    """统计导图清单、页面和资源的磁盘大小，不读取内容；未建立清单时返回 None。"""
    sizes = managed_files(Path(path))
    return sum(sizes.values()) if sizes is not None and MANIFEST in sizes else None


def directory_version(path: str | Path) -> DirectoryVersion | None:
    """在持锁恢复后读取受管文件的内容版本，不解码导图。

    路径不存在返回 None，路径类型无效、恢复或读取失败时抛出 MindMapError。
    可将返回值传给 save_directory 检查其他程序修改或删除了哪些导图文件。
    """
    try:
        root = Path(path).resolve()
        with directory_lock(root) as staging:
            recover_directory(root, staging)
            return disk_version(root)
    except MindMapError:
        raise
    except (OSError, ValueError) as exc:
        raise MindMapError(f"读取导图目录版本失败：{exc}") from exc


def check_directory_version(path: str | Path, expected: DirectoryVersion | None) -> None:
    """持锁完成恢复并检查目录版本；成员文件新增、移除或修改时抛出版本冲突。"""
    try:
        with guard_directory_version(path, expected):
            pass
    except MindMapError:
        raise
    except (OSError, ValueError) as exc:
        raise MindMapError(f"检查导图目录版本失败：{exc}") from exc


@contextmanager
def guard_directory_version(path: str | Path, expected: DirectoryVersion | None) -> Iterator[None]:
    """恢复上次写入、核对内容版本，并在调用方操作期间持有导图目录锁。

    若磁盘和预期版本不同，不进入代码块。仅用于短时本地操作，例如让 Git 提交或
    快进期间不会与其他应用实例的目录事务交错。代码块内不能再次调用需要同一把锁
    的保存、加载或版本查询；Git 自身的网络操作也应放在锁外。
    """
    root = Path(path).resolve()
    with directory_lock(root) as staging:
        recover_directory(root, staging)
        require_version(root, expected)
        yield


def load_validated_directory(path: str | Path) -> tuple[ValidatedDocument, DirectoryVersion]:
    """读取目录并返回完整校验凭据和实际读到的文件版本，尚未改变任何编辑会话。

    读取前处理未完成的保存。页面、图片或清单损坏时抛出 MindMapError。返回的
    文档由本次调用独占，调用方可只读预检后将凭据交给编辑引擎接管。
    """
    try:
        root = Path(path).resolve()
        with directory_lock(root) as staging:
            recover_directory(root, staging)
            validation, version = _decode(root)
            require_version(root, version)
            return validation, version
    except MindMapError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise MindMapError(f"读取导图目录失败：{exc}") from exc


def load_directory(path: str | Path) -> MindMapDocument:
    """从包含 mindmap.json 的目录加载完整导图，返回已校验的独立文档。"""
    validation, _ = load_validated_directory(path)
    return validation.document


def save_directory(
    document: MindMapDocument, path: str | Path, *,
    expected_version: DirectoryVersion | None | Literal[False] = False,
) -> DirectoryVersion:
    """按页写入完整快照，返回此次保存的内容版本，不修改输入文档和编辑历史。

    使用记录过的版本阻止覆盖其他程序的编辑，传入 None 要求新目录。省略时
    以持锁后的磁盘内容为基准，可用于写入新目录或更新已有导图。已存在的普通
    文件或无清单目录不能作为目标。写入只替换变化成员，失败后可恢复原快照。
    """
    try:
        root = Path(path).resolve()
        payload = _encode(document)
        version = DirectoryVersion(tuple(
            (name, len(content), hashlib.sha256(content).hexdigest()) for name, content in sorted(payload.items())
        ))
        if version.size > MAX_ARCHIVE_BYTES:
            raise MindMapError("导图目录大小超过保存限制")
        with directory_lock(root) as staging:
            recover_directory(root, staging)
            if expected_version is False:
                current = disk_version(root)
            else:
                require_version(root, expected_version)
                current = expected_version
            if current is not None and not current.has_manifest:
                raise MindMapError("目标目录已存在且缺少导图清单")
            if current is not None:
                version = _preserve_checkout_line_endings(payload, current, version)
            commit_directory(root, staging, payload, version, current)
        return version
    except MindMapError:
        raise
    except (OSError, TypeError, ValueError, RecursionError) as exc:
        raise MindMapError(f"保存导图目录失败：{exc}") from exc
