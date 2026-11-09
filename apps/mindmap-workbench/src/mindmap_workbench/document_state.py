"""工作台的最近文件、页面访问及退出会话记录；文件内容、布局由原组件持有。"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path

from mindmap_docking import JsonLayoutStore

RECENT_FILE_LIMIT = 20
RECENT_PAGE_LIMIT = 50
PageKey = tuple[str | None, str | None]


def _page_key(page_id: object, root_id: object) -> PageKey:
    """校验本机记录中的标识类型；页面是否仍存在由加载后的实际引擎判断。"""
    if any(value is not None and (not isinstance(value, str) or not value or "\0" in value)
           for value in (page_id, root_id)) or page_id is None and root_id is not None:
        raise ValueError("页面记录必须使用有效的页面和想法根标识")
    return page_id, root_id


def path_key(path: str | Path) -> str:
    """比较真实绝对路径；Windows 忽略大小写，解析软链接后避免同一文件重复编辑。"""
    return os.path.normcase(str(Path(path).resolve()))


def _document_path(value: object) -> str:
    """只接受原生导图的绝对路径，不要求文件当前存在，允许保留暂时不可用的历史。"""
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("导图路径必须是非空字符串")
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() != ".mindmap":
        raise ValueError("导图记录必须使用 .mindmap 文件的绝对路径")
    return str(path.resolve())


class DocumentState:
    """原子保存最近文件、按文件隔离的页面历史和最近一次正常退出的文件集合。

    最近文件每次更新立即落盘，但始终携带上一次成功提交的退出快照，不能把
    启动恢复期间的半成品写成新会话。新退出快照由窗口通过关闭检查后显式提交。
    页面历史先更新内存，由宿主防抖调用 flush_recent_pages；其他成功写入也会
    携带当前页面记录并清除其脏标记，避免另存为或退出时遗漏刚发生的访问。
    I/O 或格式错误交给窗口显示中文提示，文件加载不依赖此记录成功保存。
    """

    def __init__(self, path: str | Path):
        self.store = JsonLayoutStore(path)
        self.recent_files: list[str] = []
        self.open_documents: list[dict[str, object]] = []
        self.active_id = ""
        self.recent_pages: dict[str, tuple[PageKey, ...]] = {}
        self.recent_pages_dirty = False

    def load(self) -> None:
        """读取版本 1 本机记录，返回 None；路径和标识完整校验后才替换内存状态。

        可选 view_state 仅接受 JSON 对象，内部字段由所属画布按实际文档降级恢复，
        无效视图不使整份退出集合失效。旧记录缺少此字段仍可加载。不存在配置
        表示首次使用；I/O 或基本结构错误抛出 OSError/ValueError。由 GUI 线程
        的窗口初始化调用，读取结果独立持有，不借用导图或 Qt 控件的生命周期。
        可选 recent_pages 独立逐项校验，旧配置缺省为空，损坏条目不影响退出集合。
        """
        payload = self.store.load()
        if payload is None:
            return
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
            raise ValueError("不支持的导图记录版本")
        recent, documents = payload.get("recent_files"), payload.get("open_documents")
        active = payload.get("active_id", "")
        if not isinstance(recent, list) or not isinstance(documents, list) or not isinstance(active, str):
            raise ValueError("最近文件或退出会话格式无效")
        normalized_recent, normalized_documents = [], []
        recent_keys, identifiers = set(), set()
        for value in recent:
            path = _document_path(value)
            key = path_key(path)
            if key not in recent_keys:
                recent_keys.add(key)
                normalized_recent.append(path)
        for item in documents:
            if not isinstance(item, dict):
                raise ValueError("退出会话条目必须是对象")
            identifier = item.get("id")
            if not isinstance(identifier, str) or re.fullmatch(r"mindmap\.[0-9a-f]{32}", identifier) is None:
                raise ValueError("退出会话标识无效")
            path = _document_path(item.get("path"))
            # 同一路径可以有多个独立标签；稳定标签标识仍必须唯一。
            if identifier in identifiers:
                raise ValueError("退出会话包含重复的标签标识")
            identifiers.add(identifier)
            record = {"id": identifier, "path": path}
            if isinstance(item.get("view_state"), dict):
                record["view_state"] = deepcopy(item["view_state"])
            normalized_documents.append(record)
        self.recent_files = normalized_recent[:RECENT_FILE_LIMIT]
        self.open_documents = normalized_documents
        self.active_id = active if active in identifiers else ""
        # 新字段独立降级；坏路径、坏条目不影响最近文件和退出会话的有效记录。
        # 缺少 page_id 不能误认为主导图，只有显式 JSON null 才表示主导图。
        pages = payload.get("recent_pages", {})
        normalized_pages = {}
        for path, records in pages.items() if isinstance(pages, dict) else ():
            try:
                key = path_key(_document_path(path))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(records, list):
                continue
            entries = list(normalized_pages.get(key, ()))
            for record in records:
                if not isinstance(record, dict) or "page_id" not in record:
                    continue
                try:
                    entry = _page_key(record["page_id"], record.get("inspiration_root_id"))
                except ValueError:
                    continue
                if entry not in entries:
                    entries.append(entry)
                if len(entries) >= RECENT_PAGE_LIMIT:
                    break
            normalized_pages[key] = tuple(entries[:RECENT_PAGE_LIMIT])
        self.recent_pages = normalized_pages
        self.recent_pages_dirty = False

    def _save(self) -> None:
        """复用现有 JSON 存储的同目录临时文件与原子替换，写入失败保留旧文件。"""
        self.store.save({
            "schema_version": 1,
            "recent_files": self.recent_files,
            "open_documents": self.open_documents,
            "active_id": self.active_id,
            "recent_pages": {
                path: [{"page_id": page, "inspiration_root_id": root} for page, root in entries]
                for path, entries in self.recent_pages.items()
            },
        })
        self.recent_pages_dirty = False

    def pages_for(self, path: str | Path) -> tuple[PageKey, ...]:
        """读取该文件最近页面的不可变序列，不访问文件内容，也不改访问顺序。"""
        return self.recent_pages.get(path_key(path), ())

    def set_recent_pages(self, path: str | Path, pages: Sequence[PageKey]) -> bool:
        """替换一份导图的页面记录，返回是否变化；只更新内存，由宿主防抖落盘。

        path 使用与文件会话一致的规范化键；条目按调用顺序去重并保留最近 50 项。
        不持有传入容器，不修改退出快照。写入失败时仍保留这些内存记录供下次重试。
        """
        key = path_key(_document_path(str(Path(path).resolve())))
        entries = tuple(dict.fromkeys(_page_key(*item) for item in pages))[:RECENT_PAGE_LIMIT]
        if entries == self.recent_pages.get(key, ()):
            return False
        self.recent_pages[key] = entries
        self.recent_pages_dirty = True
        return True

    def flush_recent_pages(self) -> None:
        """原子写入尚未保存的页面记录；失败抛出 OSError，保留脏标记及原磁盘文件。"""
        if self.recent_pages_dirty:
            self._save()

    def remember(self, path: str | Path) -> None:
        """成功新建、打开或另存为后置顶；普通激活、自动保存及恢复不调用此接口。"""
        value = _document_path(str(Path(path).resolve()))
        key = path_key(value)
        self.recent_files = [value, *(item for item in self.recent_files if path_key(item) != key)][
            :RECENT_FILE_LIMIT
        ]
        self._save()

    def clear_recent(self) -> None:
        """只清空历史列表，保留退出快照及当前运行中的导图。"""
        self.recent_files = []
        self._save()

    def save_session(self, documents: list[dict[str, object]], active_id: str) -> None:
        """原子提交已通过全部关闭检查的退出快照，成功返回 None。

        documents 按实际分屏标签顺序提供，每项含会话 id、文件 path 和可选
        view_state；后者来自所属画布的公开快照接口。active_id 不在集合中时
        保存为空。深拷贝列表和嵌套视图，调用方之后修改参数不会改变已记住的
        状态。旧调用方只传 id/path 仍受支持，配置继续采用版本 1。

        写入失败抛出 OSError，磁盘和内存均保留上次成功提交的退出快照。由窗口
        在 GUI 线程、会话释放前调用；不访问画布或保存导图，不持有任何 Qt 对象。
        """
        previous = self.open_documents, self.active_id
        self.open_documents = deepcopy(documents)
        self.active_id = active_id if any(item["id"] == active_id for item in documents) else ""
        try:
            self._save()
        except OSError:
            self.open_documents, self.active_id = previous
            raise
