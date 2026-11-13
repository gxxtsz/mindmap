"""一份文件共享的最近页面记录；页面摘要来自引擎，持久化属于本机导图桌面。"""

from dataclasses import replace
from pathlib import Path

from mindmap_core.node_pages import parse_extension_page_id
from mindmap_desktop_api import path_key

from .page_history import RECENT_PAGE_LIMIT, PageKey


class RecentPageHistory:
    """由文件级会话持有，通过现有生命周期同步所有标签的只读侧栏。

    构造不读取或写入配置；成功打开或新建后才 bind，避免失败候选污染历史。
    全部调用位于 GUI 线程，不订阅全局事件，不创建独立计时器，也不修改引擎。
    """

    def __init__(self, document):
        self.document = document
        self.path: Path | None = None
        self.keys: tuple[PageKey, ...] = ()
        self.entries = ()

    def bind(self, path: Path, *, reset: bool = False) -> None:
        """成功绑定路径后加载历史；另存为复制内存记录，新建覆盖时显式重置。

        同文件的复制标签不重复读取或覆盖记录。更换路径只写新路径的快照，
        原路径的本机记录继续保留；此接口不会改变任何画布当前页面。
        """
        if self.path is not None and path_key(self.path) == path_key(path) and not reset:
            self.refresh()
            return
        state = self.document.owner.window.page_history
        keys = self.keys if self.path is not None else state.pages_for(path)
        self.path = Path(path)
        self.keys = () if reset else keys
        self.refresh()

    def visit(self, page_id: str | None, inspiration_root_id: str | None = None) -> None:
        """把成功显示的页面置顶；空灵感区及无效目标不制造记录。"""
        if self.path is None or self.document.disposed:
            return
        entry = self.document.engine.page_summary(page_id, inspiration_root_id)
        if entry is None:
            return
        self.keys = (entry.key, *(key for key in self.keys if key != entry.key))[:RECENT_PAGE_LIMIT]
        self.refresh()
        # 上次配置写入失败后，再次访问当前首项也应允许重试；不能仅靠排序变化
        # 才重新启动计时器。正常已保存状态不产生额外写入。
        window = self.document.owner.window
        if window.page_history.dirty:
            window.schedule_recent_pages_save()

    def refresh(self) -> None:
        """按当前已提交文档更新名称及路径，清理失效目标而不制造新的访问。

        最多查询 50 个目标及其祖先链，不读取整图快照。删除、转换或归并后的
        无效记录立即移除；撤销恢复实体本身不会重新插入尚未再次访问的页面。
        发布时各标签只同步自己的当前页，不抢焦点、切换标签或挪动画布。
        """
        if self.path is None or self.document.disposed:
            return
        self.entries = tuple(self._display_summary(entry) for key in self.keys
                             if (entry := self.document.engine.page_summary(*key)) is not None)
        self.keys = tuple(entry.key for entry in self.entries)
        window = self.document.owner.window
        if window.page_history.set_recent_pages(self.path, self.keys):
            window.schedule_recent_pages_save()
        for view in tuple(self.document.views):
            if not view.disposed:
                view.recent_pages.set_entries(self.entries)
                view.recent_pages.set_current_page(*view.current_page_key())

    def _display_summary(self, entry):
        """扩展名称由应用注册表解释，同一节点的多种详情在列表中仍可区分。"""
        target = parse_extension_page_id(entry.page_id) if entry.kind == "extension" else None
        if target is None:
            return entry
        provider = self.document.owner.window.page_registry.get(target[1])
        title = provider.title if provider else f"不可用扩展（{target[1]}）"
        return replace(entry, title=f"{entry.title} · {title}",
                       path=(*entry.path[:-1], replace(entry.path[-1], text=title)))
