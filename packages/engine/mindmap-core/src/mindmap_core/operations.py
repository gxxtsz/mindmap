"""仅修改引擎拥有的文档；事务、钩子、历史和通知由引擎统一协调。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields

from .branch_styles import apply_branch_style, branch_style_targets
from .errors import MindMapError
from .inspiration import InspirationEditorMixin
from .models import Asset, MindMapDocument, MindMapPage, Node, _resolve_detail_page, new_id
from .transactions import DocumentState


class DocumentEditor(InspirationEditorMixin):
    """在事务内部执行树、节点和图片操作，不持有引擎或注册表。"""

    def __init__(self, state: DocumentState):
        self._state = state

    @property
    def document(self) -> MindMapDocument:
        return self._state.document

    def node(self, node_id: str) -> Node:
        try:
            return self.document.nodes[node_id]
        except KeyError as exc:
            raise MindMapError(f"节点不存在：{node_id}") from exc

    def edit(self, node_id: str) -> Node:
        """验证节点存在后取得当前事务的可写副本，所有实体修改必须经过此入口。"""
        self.node(node_id)
        return self._state.edit_node(node_id)

    def side(self, node_id: str) -> str:
        """沿父链找到一级分支，仅扫描一级分支确定方向，不遍历其后代。"""
        page = self._state.page(node_id)
        root = node_id
        while root in self._state.parents:
            root = self._state.parents[root]
        current = node_id
        while current != root and self._state.parents.get(current) != root:
            current = self._state.parents[current]
        side = page.layout.direction
        if side != "both":
            return side
        counts = {"left": 0, "right": 0}
        for child in self.node(root).children:
            side = self.node(child).direction or min(counts, key=counts.get)
            counts[side] += 1
            if child == current:
                return side
        return "right"

    def roots(self, node_ids) -> list[str]:
        selected = set(node_ids)
        for node_id in selected:
            self.node(node_id)
        parents = self._state.parents
        roots = []
        for node_id in selected:
            ancestor = parents.get(node_id)
            while ancestor is not None and ancestor not in selected:
                ancestor = parents.get(ancestor)
            if ancestor is None:
                roots.append(node_id)
        if len(roots) > 1:
            def order(key):
                path = []
                while key in parents:
                    parent = parents[key]
                    path.append(self.node(parent).children.index(key))
                    key = parent
                roots_order = self.document.inspiration.root_ids
                return (roots_order.index(key) if key in roots_order else 0, *reversed(path))
            roots.sort(key=order)
        return roots

    def add_child(self, parent_id: str, text: str = "新节点", *, index: int | None = None) -> str:
        """新增子节点并返回 ID；index 为插入位置，省略时追加。"""

        parent = self.edit(parent_id)
        self.require_plain_parent(parent)
        node = Node(text)
        self._inherit_branch(node, parent)
        self._state.put_node(node)
        self._state.set_parent(node.id, parent_id)
        parent.children.insert(len(parent.children) if index is None else index, node.id)
        parent.expanded = True
        return node.id

    def add_sibling(self, node_id: str, text: str = "新节点", *, before: bool = False) -> str:
        """插入同级节点；根节点没有同级节点。"""

        parent_id = self._state.parents.get(node_id)
        if parent_id is None:
            if node_id in self.document.inspiration.root_ids:
                point = self.document.inspiration.positions[node_id]
                return self.add_inspiration(text, position=(point[0], point[1] + 100))
            raise MindMapError("根节点不能添加同级节点")
        siblings = self.edit(parent_id).children
        child = Node(text, direction=self.node(node_id).direction)
        self._inherit_branch(child, self.node(parent_id))
        self._state.put_node(child)
        self._state.set_parent(child.id, parent_id)
        siblings.insert(siblings.index(node_id) + (0 if before else 1), child.id)
        return child.id

    def insert_parent(self, node_id: str, text: str = "父节点") -> str:
        """在现有节点上方插入一级，保持原节点及其后代 ID 不变。"""

        parents = self._state.parents
        if node_id not in parents:
            if node_id in self.document.inspiration.root_ids:
                return self.group_inspirations([node_id], text,
                                               position=self.document.inspiration.positions[node_id])
            raise MindMapError("不能在根节点上方插入父节点")
        node = self.node(node_id)
        parent = Node(text, children=[node_id], direction=node.direction)
        self._inherit_branch(parent, self.node(parents[node_id]))
        parent_id = parents[node_id]
        siblings = self.edit(parent_id).children
        siblings[siblings.index(node_id)] = parent.id
        self._state.put_node(parent)
        self._state.set_parent(parent.id, parent_id)
        return parent.id

    @staticmethod
    def _inherit_branch(node: Node, parent: Node) -> None:
        """只复制父主题的分支配置，其他外观仍沿用新增节点的默认值。

        使用深副本避免后续修改子主题的嵌套样式时连带改写父主题；移动与粘贴
        不经过此函数，因而保留原有主题自己的分支配置。
        """
        if "branch" in parent.style:
            node.style["branch"] = deepcopy(parent.style["branch"])

    def set_branch_style(self, node_ids, kind, *, scope="subtree") -> None:
        """在当前事务中统一设置分支，作用范围及参数校验由共享文档操作完成。"""
        node_ids = list(node_ids)
        for key in branch_style_targets(self.document, node_ids, scope=scope):
            self.edit(key)
        apply_branch_style(self.document, node_ids, kind, scope=scope)

    def update_node(self, node_id: str, **patch) -> None:
        """修改节点内容或样式；标识、普通子列表及两类页面引用只能由结构操作维护。"""

        allowed = {item.name for item in fields(Node)} - {"id", "children", "submap_id", "detail_page_id"}
        if patch.keys() - allowed:
            raise MindMapError("包含不可直接修改的节点字段")
        node = self.edit(node_id)
        for key, value in patch.items():
            setattr(node, key, deepcopy(value))
        if "text" in patch:
            # 根节点与入口构成名称别名链；仅沿相关边传播，不扫描全部页面。
            pending, seen = [node_id], {node_id}
            while pending:
                current = self.node(pending.pop())
                linked = []
                if current.submap_id:
                    linked.append(self.document.submaps[current.submap_id].root_id)
                if current.detail_page_id:
                    linked.append(self.document.detail_pages[current.detail_page_id].root_id)
                page_id = self._state.root_pages.get(current.id)
                if page_id is not None and page_id != self.document.inspiration.id:
                    linked.append(self.document.page_record(page_id).owner_id)
                for key in linked:
                    if key not in seen:
                        seen.add(key)
                        self.edit(key).text = patch["text"]
                        pending.append(key)

    @staticmethod
    def require_plain_parent(node):
        """所有产生普通父子关系的入口共用此约束，不能借粘贴或拖动绕过。"""
        if node.submap_id:
            raise MindMapError("此节点已有子导图，请进入子导图添加内容")

    def create_submap(self, node_id):
        """叶子入口创建独立根节点和页面；名称、配置及页面引用原子提交。"""
        owner = self.node(node_id)
        self.require_page_owner(node_id)
        if owner.children or owner.submap_id:
            raise MindMapError("只有尚无子导图的叶子节点可以创建子导图")
        source = self._state.page(node_id)
        root = Node(owner.text, style=deepcopy(owner.style))
        key = new_id()
        self._state.put_node(root)
        self._state.set_page(key, MindMapPage(key, root.id, node_id, source.layout, source.theme), kind="submap")
        self.edit(node_id).submap_id = key
        return key

    def require_page_owner(self, node_id):
        """验证页面归属节点；纯图片节点及详情页内节点抛出 MindMapError。

        node_id 为本事务中的有效节点标识，找不到节点时同样抛出 MindMapError；
        成功返回 None，不提交修改。页面创建与附页粘贴共用，只在引擎所属线程、
        当前编辑事务存活期间调用，不持有事务外的节点引用。
        """
        if self.node(node_id).is_image_node:
            raise MindMapError("纯图片节点不能拥有详情页或子导图")
        if self.document.page_kind(self._state.page_id(node_id)) == "detail":
            raise MindMapError("详情页内不能拥有详情页或子导图")

    def favorite_submap_ids(self):
        """借助文档元数据查询有效收藏，保留首次出现的顺序，不枚举全文件页面。

        元数据是可扩展字段；旧程序可能保留已删除页面的 ID，或写入不认识的值。
        此处只读过滤，不在打开文件时补写或清理内容，也不收藏主导图、详情页。
        """
        values = self.document.metadata.get("submap_favorites", [])
        if not isinstance(values, list):
            return ()
        return tuple(dict.fromkeys(key for key in values
                                   if isinstance(key, str) and key in self.document.submaps))

    def _set_submap_favorite_ids(self, values):
        """通过写时复制替换收藏元数据，保留其他扩展字段及旧保存快照。"""
        metadata = self.document.metadata.copy()
        if values:
            metadata["submap_favorites"] = list(values)
        else:
            metadata.pop("submap_favorites", None)
        self._state.set_setting("metadata", metadata)

    def set_submap_favorite(self, page_id, favorite):
        """在引擎事务内校验目标及布尔参数，只在实际切换状态时记录元数据。"""
        if not isinstance(page_id, str) or page_id not in self.document.submaps:
            raise MindMapError("只能收藏当前文件中有效的子导图")
        if not isinstance(favorite, bool):
            raise MindMapError("子导图收藏状态必须为布尔值")
        values = self.favorite_submap_ids()
        if (page_id in values) == favorite:
            return
        self._set_submap_favorite_ids(
            (page_id, *values) if favorite else tuple(key for key in values if key != page_id))

    def prune_submap_favorites(self, removed):
        """清理本事务删除的子导图引用，使级联删除与收藏恢复保持原子性。"""
        values = self.document.metadata.get("submap_favorites", [])
        if isinstance(values, list):
            remaining = [key for key in values if not isinstance(key, str) or key not in removed]
            if remaining != values:
                self._set_submap_favorite_ids(remaining)

    def resolve_detail_page(self, node_id):
        """只沿入口与子导图根的别名链解析详情，不跨越普通父子关系。

        返回实际归属节点和已有详情 ID；整条链都没有详情时，返回最外层入口
        及 None，供创建操作使用。旧文件可能在链上保存多张详情：查询节点自己
        的详情优先，否则取从外到内遇到的第一张，查询不迁移或删除任何内容。
        根索引和子导图引用均为字典访问，开销只随关联链长度增长。
        """
        return _resolve_detail_page(self.document, node_id, self._state.root_pages)

    def create_detail_page(self, node_id):
        """在共享入口创建单层详情，初始外观取发起节点及其所在页。"""
        source_node = self.node(node_id)
        self.require_page_owner(node_id)
        owner_id, existing = self.resolve_detail_page(node_id)
        if existing:
            raise MindMapError("此节点已有详情页")
        source = self._state.page(node_id)
        root = Node(source_node.text, style=deepcopy(source_node.style))
        key = new_id()
        self._state.put_node(root)
        self._state.set_page(key, MindMapPage(key, root.id, owner_id, source.layout, source.theme), kind="detail")
        self.edit(owner_id).detail_page_id = key
        return key

    def remove_detail_page(self, node_id):
        """删除详情内容但保留入口和其子导图，实体及资源随同一次事务一起恢复。"""
        owner_id, page_id = self.resolve_detail_page(node_id)
        if not page_id:
            raise MindMapError("此节点没有详情页")
        keys = self.document.owned_descendants(self.document.detail_pages[page_id].root_id)
        self.edit(owner_id).detail_page_id = None
        self._remove_owned_nodes(keys)
        self._state.set_page(page_id, None)

    def _remove_owned_nodes(self, keys):
        """先删除两类页面记录，再删除实体；入口之外的页面不受同名关系影响。"""
        for key in keys:
            node = self.node(key)
            for page_id in (node.submap_id, node.detail_page_id):
                if page_id:
                    self._state.set_page(page_id, None)
        for key in keys:
            self._state.delete_node(key)

    def remove_submap(self, node_id):
        """保留入口，迭代删除拥有的全部页面与节点，资源由事务引用计数回收。"""
        owner = self.node(node_id)
        if not owner.submap_id:
            raise MindMapError("此节点没有子导图")
        root_id = self.document.submaps[owner.submap_id].root_id
        keys = self.document.owned_descendants(root_id)
        page_id = owner.submap_id
        self.edit(node_id).submap_id = None
        self._remove_owned_nodes(keys)
        self._state.set_page(page_id, None)

    def remove_nodes(self, node_ids) -> None:
        """删除选中子树；不再被节点使用的图片资源在同一事务中清理，可一起撤销恢复。"""
        node_ids = list(node_ids)

        roots = self.roots(node_ids)
        if any(key in self._state.root_pages and key not in self.document.inspiration.root_ids for key in roots):
            raise MindMapError("根节点不能删除")
        self._unlink_branches(roots)
        for node_id in roots:
            keys = self.document.owned_descendants(node_id)
            self._remove_owned_nodes(keys)

    def move_nodes(self, node_ids, target_id: str, position: str = "inside") -> None:
        """保持先序顺序移动子树；position 支持 inside、before、after。"""
        node_ids = list(node_ids)

        roots = self.roots(node_ids)
        if not roots:
            return
        self.node(target_id)
        if any(key in self._state.root_pages and key not in self.document.inspiration.root_ids for key in roots):
            raise MindMapError("根节点不能移动")
        parents = self._state.parents
        ancestor = target_id
        moving = set(roots)
        while ancestor is not None:
            if ancestor in moving:
                raise MindMapError("不能移动到自身或后代节点")
            ancestor = parents.get(ancestor)
        if position not in ("inside", "before", "after"):
            raise MindMapError("不支持的移动位置")
        if position != "inside" and target_id in self._state.root_pages:
            raise MindMapError("不能移动为根节点的同级")
        parent_id = target_id if position == "inside" else parents[target_id]
        self.require_plain_parent(self.node(parent_id))
        target_page = self._state.page_id(parent_id)
        if any(self._state.page_id(key) != target_page for key in roots):
            raise MindMapError("跨页移动请使用剪切和粘贴")
        self._unlink_branches(roots)
        parent = self.edit(parent_id)
        index = (
            len(parent.children)
            if position == "inside"
            else (parent.children.index(target_id) + (position == "after"))
        )
        parent.children[index:index] = roots
        parent.expanded = True
        for node_id in roots:
            self._state.set_parent(node_id, parent_id)
        if parent_id in self._state.root_pages and position != "inside":
            side = self.side(target_id)
            for node_id in roots:
                self.edit(node_id).direction = side if side in ("left", "right") else None

    def set_expanded(self, node_id: str, expanded: bool, *, recursive: bool = False) -> None:
        """修改折叠状态；递归模式也只产生一条历史记录。"""

        ids = self.document.descendants(node_id) if recursive else [node_id]
        for current in ids:
            self.edit(current).expanded = expanded

    def add_image_child(self, parent_id: str, data: bytes, media_type: str = "image/png", *, width: float) -> str:
        """在现有事务中追加纯图片子节点，返回新节点标识。

        parent_id 必须允许普通子节点，data/media_type/width 沿用 attach_image 的
        资源与尺寸规则；失败抛出 MindMapError，由外层事务整体回滚。本接口不解码
        图片、不持有 GUI 对象；仅供所属引擎同步调用，不得跨线程共享编辑器。
        """
        node_id = self.add_child(parent_id, "")
        self.edit(node_id).metadata["image_only"] = True
        self.attach_image(node_id, data, media_type, width=width)
        return node_id

    def attach_image(self, node_id: str, data: bytes, media_type: str = "image/png", *, width=160) -> str:
        """原子导入图片资源并关联节点，相同内容只保存一份。"""

        asset = Asset.from_bytes(data, media_type)
        self._state.set_asset(asset.id, asset)
        node = self.edit(node_id)
        node.image_id, node.image_width = asset.id, width
        return asset.id

    def copy_fragment(self, node_ids) -> MindMapDocument:
        """建立跨实例剪贴板文档，保留选中子树的层级、节点属性和所需图片资源。"""
        roots = self.roots(node_ids)
        if not roots:
            raise MindMapError("请选择需要复制的节点")
        if len({self._state.page_id(key) for key in roots}) != 1:
            raise MindMapError("不能同时复制不同页面的节点")
        selected = {x for root in roots for x in self.document.owned_descendants(root)}
        fragment = MindMapDocument.new("剪贴板")
        fragment.root.children = roots.copy()
        fragment.nodes.update({x: deepcopy(self.node(x)) for x in selected})
        # 根级自动分配方向会因复制范围改变；记录原分支方向，保持复制后的节点朝向。
        for root in roots:
            side = self.side(root)
            fragment.nodes[root].direction = side if side in ("left", "right") else None
        fragment.submaps = {page.id: deepcopy(page) for key in selected
                            if (page_id := self.node(key).submap_id)
                            for page in [self.document.submaps[page_id]]}
        fragment.detail_pages = {page.id: deepcopy(page) for key in selected
                                 if (page_id := self.node(key).detail_page_id)
                                 for page in [self.document.detail_pages[page_id]]}
        fragment.assets = {
            node.image_id: self.document.assets[node.image_id]
            for node in fragment.nodes.values()
            if node.image_id
        }
        source = self._state.page(roots[0])
        fragment.layout, fragment.theme = deepcopy(source.layout), deepcopy(source.theme)
        fragment.validate()
        return fragment

    def paste_fragment(self, parent_id: str, fragment: MindMapDocument, *, include_pages: bool = True) -> list[str]:
        """插入独立片段；include_pages=False 仅复制普通树，不改写来源或剪贴板。

        默认无损携带两类页面，向详情页粘贴附属页面时必须由宿主显式选择省略。
        先校验原片段，避免通过丢弃页面掩盖损坏输入，再在独立副本上裁剪资源。
        """
        if not isinstance(include_pages, bool):
            raise MindMapError("是否包含附属页面必须为布尔值")
        fragment = deepcopy(fragment)

        fragment.validate()
        if not include_pages:
            selected = fragment.descendants(fragment.root_id)
            fragment.nodes = {key: fragment.nodes[key] for key in selected}
            for node in fragment.nodes.values():
                node.submap_id = node.detail_page_id = None
            fragment.submaps.clear()
            fragment.detail_pages.clear()
            images = {node.image_id for node in fragment.nodes.values() if node.image_id}
            fragment.assets = {key: asset for key, asset in fragment.assets.items() if key in images}
        if fragment.submaps or fragment.detail_pages:
            self.require_page_owner(parent_id)
        parent = self.edit(parent_id)
        if fragment.root.children:
            self.require_plain_parent(parent)
        mapping = {node_id: new_id() for node_id in fragment.nodes if node_id != fragment.root_id}
        page_mapping = {page.id: new_id() for page in fragment.iter_pages()}
        for node_id, new in mapping.items():
            node = deepcopy(fragment.nodes[node_id])
            node.id, node.children = new, [mapping[x] for x in node.children]
            node.submap_id = page_mapping.get(node.submap_id)
            node.detail_page_id = page_mapping.get(node.detail_page_id)
            self._state.put_node(node)
        for page in fragment.iter_pages():
            key = page_mapping[page.id]
            self._state.set_page(key, MindMapPage(
                key, mapping[page.root_id], mapping[page.owner_id], page.layout, page.theme),
                kind=fragment.page_kind(page.id))
        roots = [mapping[x] for x in fragment.root.children]
        parent.children.extend(roots)
        parent.expanded = True
        for key in roots:
            self._state.set_parent(key, parent_id)
        for key, asset in fragment.assets.items():
            self._state.set_asset(key, asset)
        return roots

    def prune_unused_assets(self) -> None:
        """提交事务前清理无节点引用的图片；资源差异与节点差异一起进入撤销历史。"""
        doc = self.document
        referenced = {node.image_id for node in doc.nodes.values() if node.image_id}
        doc.assets = {key: value for key, value in doc.assets.items() if key in referenced}
