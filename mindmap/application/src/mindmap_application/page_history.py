"""导图页面历史；通过导图本机状态服务持久化，不承担导图桌面文件和退出会话管理。"""

from collections.abc import Sequence
from pathlib import Path

from mindmap_desktop_api import path_key
from mindmap_desktop_api.contracts import StateService

RECENT_PAGE_LIMIT = 50
PageKey = tuple[str | None, str | None]


def _page_key(page_id: object, root_id: object) -> PageKey:
    """校验记录标识；页面是否仍存在由对应导图引擎在展示时判断。"""
    if any(value is not None and (not isinstance(value, str) or not value or "\0" in value)
           for value in (page_id, root_id)) or page_id is None and root_id is not None:
        raise ValueError("页面记录必须使用有效的页面和想法根标识")
    return page_id, root_id


def _document_path(value: object) -> str:
    """只接受原生导图绝对路径；暂时离线的文件仍可保留页面历史。"""
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("导图路径必须是非空字符串")
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() != ".mindmap":
        raise ValueError("导图记录必须使用 .mindmap 文件的绝对路径")
    return path_key(path)


class PageHistoryState:
    """按文件隔离页面访问记录，所有方法由 GUI 线程调用。

    仅持有已隔离的应用状态服务，不创建配置文件或窗口。更新先留在内存，
    由应用的防抖计时器和退出流程调用 flush；失败保留脏标记以便重试。
    """

    def __init__(self, state: StateService):
        self.state = state
        self.pages: dict[str, tuple[PageKey, ...]] = {}
        self.dirty = False

    def load(self) -> None:
        """读取已有 page_history；兼容此前携带通用会话字段的版本 1 记录。

        不解释或写回那些通用字段。损坏的页面条目逐项跳过，缺少 page_id
        不视为主导图；仅显式 null 表示主导图。读取过程不触发持久化。
        """
        payload = self.state.get("page_history")
        if payload is None:
            return
        if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or
                payload["schema_version"] != 1):
            raise ValueError("不支持的页面历史版本")
        pages = payload.get("recent_pages", {})
        normalized = {}
        for path, records in pages.items() if isinstance(pages, dict) else ():
            try:
                key = _document_path(path)
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(records, list):
                continue
            entries = list(normalized.get(key, ()))
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
            normalized[key] = tuple(entries[:RECENT_PAGE_LIMIT])
        self.pages = normalized
        self.dirty = False

    def pages_for(self, path: str | Path) -> tuple[PageKey, ...]:
        """返回该文件页面标识的不可变快照，不读取导图正文或改变访问顺序。"""
        return self.pages.get(path_key(path), ())

    def set_recent_pages(self, path: str | Path, pages: Sequence[PageKey]) -> bool:
        """替换内存快照，规范化文件路径并按顺序去重，最多保留 50 项。"""
        key = _document_path(str(Path(path).resolve()))
        entries = tuple(dict.fromkeys(_page_key(*item) for item in pages))[:RECENT_PAGE_LIMIT]
        if entries == self.pages.get(key, ()):
            return False
        self.pages[key] = entries
        self.dirty = True
        return True

    def flush(self) -> None:
        """只提交页面历史字段；写盘失败时保留内存和脏标记，平台旧文件保持完整。"""
        if not self.dirty:
            return
        self.state.set("page_history", {
            "schema_version": 1,
            "recent_pages": {
                path: [{"page_id": page, "inspiration_root_id": root} for page, root in entries]
                for path, entries in self.pages.items()
            },
        })
        self.state.flush()
        self.dirty = False
