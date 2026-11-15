"""原生导图目录编解码；按页保存原始实体，读取后统一校验跨页关系。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from mindmap_core import (
    InspirationArea,
    MindMapDocument,
    MindMapError,
    MindMapPage,
    MindMapPageView,
    Node,
    SaveSnapshot,
    ValidatedDocument,
)
from mindmap_core.node_pages import GRAPH_PAGE_TYPE, graph_members

from .directory_storage import (
    MANIFEST,
    DirectoryVersion,
    DirectoryVersionConflict,
    commit_directory,
    directory_lock,
    disk_version,
    is_directory_member,
    managed_files,
    recover_directory,
    require_version,
)
from .json_codec import _parse_json
from .limits import MAX_ARCHIVE_BYTES
from .schema import SCHEMA_VERSION, _from_dict, _node_record

_DIRECTORY_FORMAT = "mindmap-directory"
_DIRECTORY_VERSION = 1
_PAGE_GROUPS = ("submaps", "detail_pages")
_FIXED_PAGES = ("pages/main.json", "pages/inspiration.json")
_PageRecord = MindMapPage | InspirationArea | None


@dataclass(frozen=True, slots=True)
class DirectoryUpdate:
    """相对已落盘版本的目录更新，可直接传给后台进程及保存接口。

    pages 借用本次需要编码的页面记录和原始节点，manifest 为需重写的清单，
    assets 与 removed 分别记录新增或替换的资源字节和应删除的文件名。各键均为
    目录内的规范路径。调用方通过 prepare_directory_update 创建并按只读数据传递，
    保存时用 expected_version 核对包括未变化成员在内的整份磁盘版本。
    """

    pages: dict[str, tuple[_PageRecord, tuple[Node, ...]]]
    manifest: dict | None
    assets: dict[str, bytes]
    removed: frozenset[str]
    expected_version: DirectoryVersion


def _page_file(page_id: str) -> str:
    # 现有 UUID 可直接用于文件名。其他合法 ID 仍保持原值，并使用摘要作为文件名，
    # 避免路径分隔符、Windows 保留名称、大小写碰撞及文件名长度上限。
    key = (page_id if re.fullmatch(r"[a-f0-9]{32}", page_id)
           else hashlib.sha256(page_id.encode("utf-8", errors="surrogatepass")).hexdigest())
    return f"pages/{key}.json"


def _page_nodes(document: MindMapDocument, roots: list[str]) -> Iterator[Node]:
    """从原始实体收集本页节点及图内文档节点，附属树页由各自的根单独收集。"""
    stack = roots.copy()
    while stack:
        node = document.nodes[stack.pop()]
        yield node
        stack.extend(node.children)
        if (graph := node.extension_pages.get(GRAPH_PAGE_TYPE)) is not None:
            stack.extend(graph_members(graph))


def _page_sources(document: MindMapDocument) -> dict[str, tuple[_PageRecord, list[str]]]:
    """按目录文件划分主图、灵感区和附属树页。节点图随其所在页面编码。"""
    return {
        _FIXED_PAGES[0]: (None, [document.root_id]),
        _FIXED_PAGES[1]: (document.inspiration, document.inspiration.root_ids),
        **{_page_file(page.id): (page, [page.root_id]) for page in document.iter_pages()},
    }


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
        if old is not None and old[0] > size and name in payload and name.endswith(".json"):
            content = payload[name]
            if old[0] == size + content.count(b"\n"):
                crlf_digest = hashlib.sha256(content.replace(b"\n", b"\r\n")).hexdigest()
                if old[1] == crlf_digest:
                    size, digest = old
        entries.append((name, size, digest))
    return DirectoryVersion(tuple(entries))


def _manifest_record(document: MindMapDocument) -> dict:
    return {
        "format": _DIRECTORY_FORMAT,
        "schema_version": _DIRECTORY_VERSION,
        "root_id": document.root_id,
        "assets": [{"id": key, "media_type": asset.media_type, "path": f"assets/{key}"}
                   for key, asset in sorted(document.assets.items())],
        "layout": asdict(document.layout),
        "theme": asdict(document.theme),
        "metadata": document.metadata,
        "submaps": list(document.submaps),
        "detail_pages": list(document.detail_pages),
    }


def _encode_page(page: _PageRecord, nodes: Iterable[Node]) -> bytes:
    record = {"page": asdict(page)} if page is not None else {}
    record["nodes"] = [_node_record(node) for node in sorted(nodes, key=lambda item: item.id)]
    return _json_bytes(record)


def prepare_directory_update(
    snapshot: SaveSnapshot, previous: SaveSnapshot, expected_version: DirectoryVersion,
) -> DirectoryUpdate | None:
    """根据当前快照与最近一次打开或成功落盘的快照准备增量保存。

    调用方须传入同一路径对应的 previous 和 expected_version。仅接受同一引擎、
    同次装载中的标准文档，其他情况返回 None 以使用完整保存。快照经过装载或编辑
    校验，写时复制使未变化节点可以按引用快速比较。返回的更新借用当前快照实体，
    可交给同步保存或后台进程编码，失败后可以继续以原落盘版本为基准准备更新。
    """
    document, saved = snapshot.document, previous.document
    if (snapshot._owner is not previous._owner or snapshot._generation != previous._generation
            or type(document) is not MindMapDocument or type(saved) is not MindMapDocument
            or not expected_version.has_manifest):
        return None

    sources, old_sources = _page_sources(document), _page_sources(saved)
    pages = {}
    for name, (page, roots) in sources.items():
        nodes = tuple(_page_nodes(document, roots))
        # 页根决定起点，页内父节点和图入口决定成员。跨页转移或删除时，即使
        # 后代实体沿用旧对象，改变根或成员关系的节点也会使来源页和目标页入选。
        if old_sources.get(name) != (page, roots) or any(
            (old := saved.nodes.get(node.id)) is not node and old != node for node in nodes
        ):
            pages[name] = (page, nodes)

    assets = {f"assets/{key}": asset.data for key, asset in document.assets.items()
              if saved.assets.get(key) != asset}
    removed_assets = saved.assets.keys() - document.assets.keys()
    removed = old_sources.keys() - sources.keys()
    removed.update(f"assets/{key}" for key in removed_assets)
    manifest = None
    if (assets or removed_assets or document.root_id != saved.root_id
            or document.layout != saved.layout or document.theme != saved.theme
            or document.metadata != saved.metadata or list(document.submaps) != list(saved.submaps)
            or list(document.detail_pages) != list(saved.detail_pages)):
        manifest = _manifest_record(document)
    return DirectoryUpdate(pages, manifest, assets, frozenset(removed), expected_version)


def _encode(document: MindMapDocument) -> dict[str, bytes]:
    if isinstance(document, MindMapPageView):
        raise MindMapError("单页读模型不能保存为完整文件，请使用引擎的完整文档快照")
    document.validate()
    payload = {MANIFEST: _json_bytes(_manifest_record(document))}
    payload.update((name, _encode_page(page, _page_nodes(document, roots)))
                   for name, (page, roots) in _page_sources(document).items())
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
    for name, (_, roots) in _page_sources(document).items():
        if membership[name] != sorted(node.id for node in _page_nodes(document, roots)):
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
    document: MindMapDocument | DirectoryUpdate, path: str | Path, *,
    expected_version: DirectoryVersion | None | Literal[False] = False,
) -> DirectoryVersion:
    """按页写入完整快照或目录更新，返回所有受管文件此次保存后的内容版本。

    完整快照使用 expected_version 阻止覆盖外部修改，传入 None 要求创建新目录，
    省略时以持锁后的磁盘内容为基准。目录更新使用准备时绑定的磁盘版本，只编码
    受影响的页面、清单和资源，并复用其他文件的摘要。两种写入均通过同一事务
    提交，失败后可恢复原快照。调用方收到结果后负责更新自己的基准及保存标记。
    """
    try:
        root = Path(path).resolve()
        entries = {}
        if isinstance(document, DirectoryUpdate):
            if expected_version is not False and expected_version != document.expected_version:
                raise MindMapError("目录更新的保存基准与预期版本不符")
            expected_version = document.expected_version
            entries.update((name, (size, digest)) for name, size, digest in expected_version.files
                           if name not in document.removed)
            payload = {name: _encode_page(*page) for name, page in document.pages.items()}
            if document.manifest is not None:
                payload[MANIFEST] = _json_bytes(document.manifest)
            payload.update(document.assets)
            if (not all(is_directory_member(name) for name in payload)
                    or document.removed.intersection((MANIFEST, *_FIXED_PAGES))):
                raise MindMapError("目录更新包含无效的文件变更")
        else:
            payload = _encode(document)
        entries.update((name, (len(content), hashlib.sha256(content).hexdigest()))
                       for name, content in payload.items())
        version = DirectoryVersion(tuple(
            (name, size, digest) for name, (size, digest) in sorted(entries.items())
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
