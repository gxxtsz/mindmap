"""画布节点的剪贴板命令与外部大纲粘贴流程。"""

from copy import copy, deepcopy

from mindmap_core import MindMapDocument, MindMapError
from mindmap_core.formats import decode_document, detect_outline_format, encode_document
from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QApplication, QMessageBox

from ..controls.dialogs import EdrawPasteOptions, edraw_paste_options
from .clipboard import (
    CLIPBOARD_MIME,
    EDRAW_CLIPBOARD_MIME,
    has_clipboard_image,
    read_clipboard_image,
    read_edraw_clipboard,
)
from .interactions import document_interaction


class ClipboardActionsMixin:
    """在 GUI 线程读取剪贴板，并通过画布和引擎提交有效的节点改动。

    格式识别、粘贴选项和候选布局检查在提交前完成。剪贴板载荷与
    弹窗由 Qt 管理，实际文档修改仍进入画布所属引擎的事务与历史。
    """

    @property
    def edraw_paste_options(self) -> tuple[str, bool, bool]:
        """返回亿图、缩进文本与 Markdown 共用的粘贴选项，初始为 (append, True, True)。

        三项分别为粘贴方式、保留原后代及替换根节点；接口名沿用亿图版本保持兼容。
        此值只属于当前组件，不进入导图文件或撤销历史。宿主可监听
        edraw_paste_options_changed(str, bool, bool) 保存，再通过 setter 恢复跨重启偏好。
        根节点指当前选中的目标主题；三种来源每次均询问，多根追加不会覆盖单树偏好。
        """
        return self._edraw_paste_options

    def set_edraw_paste_options(self, mode: str, keep_children: bool, replace_root: bool = True) -> None:
        """设置三种外部大纲共用的默认选项；非法值抛出 MindMapError，不修改文档。

        mode 仅允许 append、replace，keep_children 和 replace_root 必须是布尔值。
        replace_root 默认 True，已有二参数调用仍沿用替换目标正文的行为。实际改变时
        才发送三参数信号，适合宿主 QSettings 持久化；恢复偏好不受只读或编辑状态限制。
        沿用亿图接口名和信号，宿主继续使用既有配置项同步所有画布，无需迁移。
        """
        if (mode not in ("append", "replace") or not isinstance(keep_children, bool)
                or not isinstance(replace_root, bool)):
            raise MindMapError("粘贴方式必须为 append 或 replace，保留后代及替换根节点选项必须为布尔值")
        options = (mode, keep_children, replace_root)
        if options != self._edraw_paste_options:
            self._edraw_paste_options = options
            self.edraw_paste_options_changed.emit(*options)

    def copy(self):
        """使用注入的 JSON 与 Markdown 格式导出片段，文件与剪贴板共享解析协议。"""
        if self.graph is not None:
            return self._copy_graph_nodes()
        fragment = self.engine.copy_fragment(self.selected_node_ids())
        mime = QMimeData()
        mime.setData(
            CLIPBOARD_MIME, encode_document(fragment, registry=self.format_registry, format_id="json")
        )
        # 专用格式保存原文，纯文本剪贴板则应与画布一致。只有片段外引用需要
        # 额外投影；单页副本不会把投影文字误写回附属页的所有权关系或原片段。
        outline = fragment
        for key, node in fragment.nodes.items():
            source = self._book_document.nodes.get(key)
            if node.page_reference_root_id is None and source is not None and source.page_reference_root_id:
                if outline is fragment:
                    outline = fragment.page_view()
                if key in outline.nodes:
                    body = self._book_document.display_node(key)
                    shown = copy(outline.nodes[key])
                    shown.text, shown.text_format = body.text, body.text_format
                    outline.nodes[key] = shown
        mime.setText(encode_document(outline, registry=self.format_registry, format_id="markdown").decode("utf-8"))
        QApplication.clipboard().setMimeData(mime)

    @document_interaction
    def cut(self):
        """先按注入格式复制当前选择，再删除相应子树；只读或选中根节点时拒绝。

        剪贴板编码失败时不会执行删除；正式删除使用引擎的历史与资源清理规则。
        """
        self._ensure_editable()
        if self.graph is not None:
            if self.selection_count:
                self._copy_graph_nodes()
                self._delete_graph_nodes(self.selected_node_ids())
            return
        if self.current_page_kind != "inspiration" and self.is_node_selected(self._document.root_id):
            raise MindMapError("不能剪切根节点")
        self.copy()
        self.engine.remove_nodes(self.selected_node_ids())

    @document_interaction
    def paste(self) -> list[str]:
        """同步粘贴节点片段、图片或文本，返回本次应选中的主题标识列表。

        无参数；按项目片段、亿图片段、图像、文本的优先级解析，无可处理内容返回空列表。
        图片在编辑中的节点或 current_node_id() 下追加纯图片子节点，多选仅处理首个父节点。
        图片同时支持位图数据及资源管理器复制的单张本地图片文件；纯文字路径仍按文本解析。
        图片校验通过后才提交有效草稿；输入法候选、只读、失效目标或坏图抛出 MindMapError，
        不改变草稿、文档或历史。成功后选中新图片子节点，创建独立参与撤销、重做及保存。
        片段继续使用注入的格式注册表，返回新增标识并选中它们；格式及文件读取错误向上传递。
        文本按首个非空行自动识别 Markdown 或缩进大纲；缩进文本以 as_fragment=True
        委托注入的编解码器解析多根片段。忽略 BOM 和空白行后仅有一行、且解析片段
        仅含一个正文节点时，直接追加为子节点，不读取或更新共享偏好；Markdown
        仍按原规则去除标题或列表标记。多行或扩展导入器返回多节点时，完整校验后
        显示与亿图共用偏好的配置弹窗；目标定位沿用 current_node_id()。
        追加或替换共用一次撤销记录；替换时返回目标标识，追加时返回新增顶层标识。
        灵感区无选择时直接创建游离树，不显示选项或更新共享偏好。
        文本格式确定后不降级重试，缩进及层级错误包含原文行号，失败不改变选择或历史。
        仅在 GUI 线程、画布释放前调用；剪贴板载荷只在同步调用期间借用，不保留其所有权。
        界面入口通过 call() 将异常交给统一错误信号，普通文本编辑仍由文本控件处理。
        """
        self._ensure_editable()
        if self.graph is not None:
            return self._paste_graph_nodes()
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            return []
        outline_title = None
        show_outline_options = True
        if mime.hasFormat(CLIPBOARD_MIME):
            fragment = decode_document(bytes(mime.data(CLIPBOARD_MIME)), registry=self.format_registry)
        elif mime.hasFormat(EDRAW_CLIPBOARD_MIME):
            # 先读完并验证结构，再询问放置方式；亿图解析失败时不能降级为纯文本。
            fragment = read_edraw_clipboard(bytes(mime.data(EDRAW_CLIPBOARD_MIME)))
            if self.current_page_kind != "inspiration" or self.selection_count:
                return self._paste_edraw(fragment)
        elif has_clipboard_image(mime):
            return self._paste_image(mime)
        elif mime.hasText():
            text = mime.text()
            format_id = detect_outline_format(text)
            outline_title = "粘贴缩进大纲" if format_id == "text" else "粘贴 Markdown 大纲"
            document = decode_document(
                text.encode("utf-8"),
                registry=self.format_registry,
                format_id=format_id,
                options={"as_fragment": True} if format_id == "text" else None,
            )
            if format_id == "text":
                # 纯文本编码器已返回带合成根的片段，不能再次包装而增加可见容器节点。
                fragment = document
            else:
                fragment = MindMapDocument.new("剪贴板")
                fragment.root.children = [document.root_id]
                fragment.nodes.update(document.nodes)
            # 原文和解析结果必须同时满足单行单节点，避免把扩展导入器返回的树或森林
            # 当作普通文字直贴；仍通过注入的编解码器解析和校验，不自行创建正文节点。
            show_outline_options = not (
                sum(bool(line.strip()) for line in text.removeprefix("\ufeff").splitlines()) == 1
                and len(fragment.nodes) == 2
                and len(fragment.root.children) == 1
            )
        else:
            return []
        include_pages = True
        if self.current_page_kind == "detail" and (fragment.submaps or fragment.detail_pages):
            target_id = self.current_node_id()
            generation, revision = self.engine.document_generation, self.engine.revision
            # 明确说明只省略附属页面；关闭或取消不改写片段、当前选择或撤销历史。
            choice = QMessageBox.question(
                self, "粘贴到详情页",
                f"所复制的分支包含 {len(fragment.submaps)} 张子导图和 {len(fragment.detail_pages)} 张详情页。\n"
                "详情页内不能拥有附属页面。是否仅粘贴普通分支、文字、图片和样式？\n"
                "附属页面将不会粘贴，剪贴板内容保持完整。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if choice != QMessageBox.StandardButton.Yes:
                return []
            self._ensure_editable()
            if (self._disposed or (generation, revision) != (
                    self.engine.document_generation, self.engine.revision)
                    or self.current_page_kind != "detail" or self.current_node_id() != target_id):
                raise MindMapError("粘贴期间导图或目标已发生变化，请重新粘贴")
            include_pages = False
        if outline_title is not None and (self.current_page_kind != "inspiration" or self.selection_count):
            return self._paste_outline(
                fragment, title=outline_title, include_pages=include_pages, show_options=show_outline_options,
            )
        revision = self.engine.revision
        if self.current_page_kind == "inspiration" and not self.selection_count:
            with self.engine.transaction("粘贴游离分支"):
                container = self.engine.add_inspiration("剪贴板临时容器", position=(0, 0))
                ids = self.engine.paste_fragment(container, fragment, include_pages=include_pages)
                candidate = self.engine.page_document(self.engine.inspiration_id)
                self.engine.move_to_inspiration(ids, positions=self.inspiration_positions(ids, candidate))
                self.engine.remove_nodes([container])
        else:
            ids = self.engine.paste_fragment(self.current_node_id(), fragment, include_pages=include_pages)
        self.select_nodes(ids)
        if self.engine.revision != revision:
            self._update_history_view_after()
        return ids

    def _paste_editor_image(self) -> bool:
        """尝试接管正文编辑器的图像粘贴，返回是否已经消费本次快捷键。

        无参数，仅由有效编辑器在 GUI 按键事件内同步调用。非图像或带专用节点片段时
        返回 False，保留文本控件原行为；图像返回 True，失败通过 call() 发出错误信号，
        避免坏图或输入法候选被误当成文本。MIME 对象仅借用到本次调用结束，不缓存。
        """
        if self.graph is not None:
            return False
        mime = QApplication.clipboard().mimeData()
        if (mime is None or mime.hasFormat(CLIPBOARD_MIME) or mime.hasFormat(EDRAW_CLIPBOARD_MIME)
                or not has_clipboard_image(mime)):
            return False
        self.call(lambda: self._paste_image(mime))
        return True

    def _paste_image(self, mime: QMimeData) -> list[str]:
        """校验借用的图像载荷后创建子节点，返回单个新增标识或无图时的空列表。

        仅在 GUI 线程、组件存活期间同步执行。只读、候选输入、目标或文档失效及图片
        解码错误抛出 MindMapError；读取并验证图片前不提交任何草稿。有效草稿沿用各自
        历史，随后图片子节点独立提交；资源生命周期由引擎负责。调用不保留 MIME 引用。
        """
        self._ensure_editable()
        if self.current_page_kind == "inspiration" and not self.selection_count and self.editor is None:
            raise MindMapError("请先选择父主题，再粘贴图片")
        target_id = self.editor.node_id if self.editor is not None else self.current_node_id()
        page_id, generation = self.current_page_id, self.engine.document_generation

        def check_target():
            self._ensure_editable()
            if (self._disposed or generation != self.engine.document_generation
                    or page_id != self.current_page_id or target_id not in self._document.nodes):
                raise MindMapError("粘贴期间导图或目标已发生变化，请重新粘贴")
            if self.is_composing or not all(guard() for guard in tuple(self._navigation_guards)):
                raise MindMapError("请先完成输入法候选，再粘贴图片")
            self._check_image_parent(target_id)

        check_target()
        image = read_clipboard_image(mime)
        if image is None:
            return []
        check_target()
        return [self._insert_image_child(target_id, *image)]

    def _paste_edraw(self, fragment: MindMapDocument) -> list[str]:
        """兼容原亿图入口；委托共用流程收集选项并一次提交片段、选择和共享偏好。"""
        return self._paste_outline(fragment, title="粘贴亿图脑图")

    def _paste_outline(
        self, fragment: MindMapDocument, *, title: str, include_pages: bool = True,
        show_options: bool = True,
    ) -> list[str]:
        """按需询问外部大纲的放置方式，原子地追加或替换，返回应选中的主题标识。

        fragment 为带合成根的独立片段，先校验再弹窗；title 用于来源标题及事务说明。
        include_pages 沿用主粘贴入口的附属页面确认结果，False 时仅导入普通分支。
        show_options 默认 True，显示配置并使用共享偏好；False 仅由 paste 已确认的
        单行单节点文本使用，固定追加且不读取、修改偏好或发送偏好变更信号。
        单根允许替换目标正文或仅导入子树，原有后代可独立保留；多根仅允许追加。
        片段只在副本上继承目标分支样式，调用方的数据及系统剪贴板始终保持原样。
        取消返回空列表；只读、失效现场、非法选项或引擎拒绝抛出中文 MindMapError，
        本次操作不改变文档、选择、历史或偏好。确认后重新检查文档代次、修订、页面
        与目标；成功后以一次事务提交，并且只为经过配置确认的单根粘贴记忆共享偏好。
        无内容变化不新增撤销记录，但成功确认的单根选项仍可记忆。仅在 GUI 线程、
        画布有效且具有目标时调用；灵感区无选择的游离树创建由 paste 另行处理。
        """
        self._ensure_editable()
        fragment.validate()
        target_id = self.current_node_id()
        target = self.engine.get_node(target_id)
        can_replace_root = target.page_reference_root_id is None
        page_id = self.current_page_id
        generation, revision = self.engine.document_generation, self.engine.revision
        roots = fragment.root.children
        if show_options:
            mode, keep_children, replace_root = self.edraw_paste_options
            choice = edraw_paste_options(
                self, target_text=self._document.nodes[target_id].text,
                root_count=len(roots), node_count=len(fragment.nodes) - 1,
                mode=mode, keep_children=keep_children, replace_root=replace_root,
                theme=self._ui_colors_override or self._document.theme, title=title,
                can_replace_root=can_replace_root,
            )
        else:
            # 单行直贴固定追加；不借用共享默认值，避免上一次替换选择覆盖目标或后代。
            choice = EdrawPasteOptions("append", True, True)
        if choice is None:
            return []
        self._ensure_editable()
        # 模态弹窗仍会处理宿主事件，即使未修改内容，切页或切换目标也会使本次选择失效。
        if self._disposed or (generation, revision) != (
            self.engine.document_generation, self.engine.revision
        ) or self.current_page_id != page_id or self.current_node_id() != target_id:
            raise MindMapError("粘贴期间导图、页面或目标已发生变化，请重新粘贴")
        if (choice.mode not in ("append", "replace") or not isinstance(choice.keep_children, bool)
                or not isinstance(choice.replace_root, bool)):
            raise MindMapError("粘贴选项无效")
        if choice.mode == "replace" and len(roots) != 1:
            raise MindMapError("多个并列主题只能追加到选中节点下")
        if choice.mode == "replace" and choice.replace_root and not can_replace_root:
            raise MindMapError("请先解除页面引用，再替换节点正文")

        # 新主题沿用正常 add_child 的分支继承规则，字体、颜色、方向等保持项目默认值。
        # 每个节点分别深拷贝，避免之后编辑某条分支时连带改变其他新主题。
        fragment = deepcopy(fragment)
        if "branch" in target.style:
            for node in fragment.nodes.values():
                if node.id != fragment.root_id:
                    node.style["branch"] = deepcopy(target.style["branch"])
        with self.engine.transaction(title):
            if choice.mode == "replace":
                source = fragment.nodes[roots[0]]
                if not choice.keep_children:
                    self.engine.remove_nodes(target.children)
                # 保留根节点时完全跳过正文更新，保留文字格式，也避免触发附属页面的名称同步。
                # 根节点选择只影响目标正文；后代删除、导入仍由独立选项控制并共用一次事务。
                if choice.replace_root:
                    self.engine.update_node(target_id, text=source.text, text_format="plain")
                # paste_fragment 忽略片段根；把来源根作为合成根即可只插入它的后代。
                # 目标 ID、同级位置和本项目属性始终保留；保留的旧后代排在新后代之前。
                replacement = deepcopy(fragment)
                del replacement.nodes[replacement.root_id]
                replacement.root_id = source.id
                self.engine.paste_fragment(target_id, replacement, include_pages=include_pages)
                ids = [target_id]
            else:
                ids = self.engine.paste_fragment(target_id, fragment, include_pages=include_pages)
        self.select_nodes(ids)
        # 引擎通知默认选中新插入的后代；替换后应选中目标，重做时也必须恢复同一选择。
        if self.engine.revision != revision and self._selection_undo:
            before, _ = self._selection_undo[-1]
            self._selection_undo[-1] = (before, list(ids))
            self._update_history_view_after()
        # 仅在确认过配置的单树粘贴完整成功后记忆选择；单行直贴与多根均不改写偏好。
        if show_options and len(roots) == 1:
            self.set_edraw_paste_options(choice.mode, choice.keep_children,
                                         choice.replace_root if can_replace_root else replace_root)
        return ids
