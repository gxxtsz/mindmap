"""灵感展示上下文、本机视图快照和与内容历史对应的界面现场。"""

from collections.abc import Mapping

from mindmap_core import MindMapError
from PySide6.QtCore import QPointF
from PySide6.QtGui import QTransform

from .navigation import PageViewState


class InspirationViewStateMixin:
    """扩展页面视图快照；这些状态从不写入 .mindmap 内容或引擎历史。"""

    def capture_page_view_state(self) -> dict[str, object]:
        """捕获当前页和已访问灵感视图，返回调用方独立持有的 JSON 兼容对象。

        普通页原有五个字段保持原语义；访问过独立想法时追加 inspiration，包含
        当前根、最近根及逐想法视口，不再写展示模式。不会复制内容、提交输入或制造历史。
        仅供对应文件的本机正常退出恢复；GUI 线程调用，释放后抛出 MindMapError。
        """
        result = super().capture_page_view_state()
        views = []
        for key, state in self._page_states.items():
            if not isinstance(key, tuple) or key[0] != self.engine.inspiration_id:
                continue
            _, root = key
            if root is not None and root not in self._book_document.inspiration.root_ids:
                continue
            views.append({"root_id": root,
                          "zoom": state.transform.m11(),
                          "center": [state.center.x(), state.center.y()],
                          "selection": list(state.selection), "focus_id": state.focus_id})
        if views:
            result["inspiration"] = {"root_id": self._inspiration_root_id,
                                     "last_root_id": self._last_inspiration_root, "views": views}
        return result

    def restore_page_view_state(self, state: Mapping[str, object]) -> None:
        """恢复当前页及逐想法视口，兼容旧本机记录，返回 None。

        旧总览视口整体忽略，转为最近有效想法，否则首项或空态；保留旧记录中
        有效的单想法视口，不把多树缩放、坐标或选择套到单树。新记录按稳定根
        标识匹配，失效记录跳过。当前失效根回退最近有效想法或首项；无效单树
        视口沿用普通页倍率与坐标降级规则，不把旧模式字段重新写入新快照。
        不借用参数、不修改文档；只读允许，正文编辑或输入法候选时整体拒绝。
        宿主须在加载对应文件后、GUI 线程且组件存活时调用。
        """
        if not isinstance(state, Mapping):
            raise MindMapError("页面视图快照必须是对象")
        if self._disposed:
            raise MindMapError("画布已释放，不能恢复页面视图")
        if self.editor is not None or self.is_composing or not all(
                guard() for guard in tuple(self._navigation_guards)):
            raise MindMapError("请先结束编辑和输入法候选，再恢复页面视图")
        extra = state.get("inspiration")
        target = dict(state)
        roots = self._book_document.inspiration.root_ids
        last = self._last_inspiration_root
        if isinstance(extra, Mapping):
            recorded_last = extra.get("last_root_id")
            if isinstance(recorded_last, str) and recorded_last in roots:
                last = recorded_last
            records = extra.get("views")
            for entry in records if isinstance(records, list) else ():
                if not isinstance(entry, Mapping) or entry.get("mode") not in (None, "single"):
                    continue
                root = entry.get("root_id")
                if not (isinstance(root, str) and root in roots or root is None and not roots):
                    continue
                super().restore_page_view_state({**entry, "page_id": self.engine.inspiration_id,
                                                 "inspiration_root_id": root})
            target["inspiration_root_id"] = extra.get("root_id")
        self._last_inspiration_root = last if last in roots else None
        if state.get("page_id") == self.engine.inspiration_id:
            legacy_overview = (state.get("inspiration_mode") == "overview"
                               or isinstance(extra, Mapping) and extra.get("mode") == "overview"
                               or not isinstance(extra, Mapping) and "inspiration_root_id" not in state)
            preferred = None if legacy_overview else target.get("inspiration_root_id")
            root = self._choose_inspiration_root(preferred if isinstance(preferred, str) else None)
            if legacy_overview or preferred != root:
                # 总览或已失效想法的原始几何不能套用到另一棵树；优先恢复目标树缓存。
                cached = self._page_states.get((self.engine.inspiration_id, root))
                target.update({"zoom": cached.transform.m11() if cached else None,
                               "center": [cached.center.x(), cached.center.y()] if cached else None,
                               "selection": list(cached.selection) if cached else None,
                               "focus_id": cached.focus_id if cached else None})
            target["inspiration_root_id"] = root
        super().restore_page_view_state(target)

    def _history_view_snapshot(self):
        self._cache_page_view()
        state = self._page_states[self._view_key()]
        # 独立保留 Qt 值对象，后续平移和选择不会改写已经记录的历史现场。
        return (self.current_page_id, self._inspiration_root_id,
                PageViewState(QTransform(state.transform), QPointF(state.center), state.selection, state.focus_id))

    def _restore_history_view(self, snapshot):
        page, root, state = snapshot
        if not self._book_document.has_page(page):
            return
        if page == self.engine.inspiration_id:
            root = self._choose_inspiration_root(root)
        key = (page, root) if page == self.engine.inspiration_id else page
        self._page_states[key] = state
        self._switch_page(page, cache=False, notify=False, inspiration_root_id=root)
