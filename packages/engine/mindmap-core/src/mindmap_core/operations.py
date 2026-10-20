"""仅修改引擎拥有的文档；事务、钩子、历史和通知由引擎统一协调。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields

from .branch_styles import apply_branch_style
from .errors import MindMapError
from .models import Asset, MindMapDocument, Node, new_id
from .transactions import DocumentState


class DocumentEditor:
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

    def roots(self, node_ids) -> list[str]:
        selected = set(node_ids)
        for node_id in selected:
            self.node(node_id)
        parents = self.document.parents()
        roots = []
        for node_id in self.document.descendants(self.document.root_id):
            if node_id not in selected:
                continue
            ancestor = parents.get(node_id)
            while ancestor is not None and ancestor not in selected:
                ancestor = parents.get(ancestor)
            if ancestor is None:
                roots.append(node_id)
        return roots

    def add_child(self, parent_id: str, text: str = "新节点", *, index: int | None = None) -> str:
        """新增子节点并返回 ID；index 为插入位置，省略时追加。"""

        parent = self.node(parent_id)
        node = Node(text)
        self._inherit_branch(node, parent)
        self.document.nodes[node.id] = node
        parent.children.insert(len(parent.children) if index is None else index, node.id)
        parent.expanded = True
        return node.id

    def add_sibling(self, node_id: str, text: str = "新节点", *, before: bool = False) -> str:
        """插入同级节点；根节点没有同级节点。"""

        parent_id = self.document.parents().get(node_id)
        if parent_id is None:
            raise MindMapError("根节点不能添加同级节点")
        siblings = self.node(parent_id).children
        child = Node(text, direction=self.node(node_id).direction)
        self._inherit_branch(child, self.node(parent_id))
        self.document.nodes[child.id] = child
        siblings.insert(siblings.index(node_id) + (0 if before else 1), child.id)
        return child.id

    def insert_parent(self, node_id: str, text: str = "父节点") -> str:
        """在现有节点上方插入一级，保持原节点及其后代 ID 不变。"""

        parents = self.document.parents()
        if node_id not in parents:
            raise MindMapError("不能在根节点上方插入父节点")
        node = self.node(node_id)
        parent = Node(text, children=[node_id], direction=node.direction)
        self._inherit_branch(parent, self.node(parents[node_id]))
        siblings = self.node(parents[node_id]).children
        siblings[siblings.index(node_id)] = parent.id
        self.document.nodes[parent.id] = parent
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
        apply_branch_style(self.document, node_ids, kind, scope=scope)

    def update_node(self, node_id: str, **patch) -> None:
        """修改节点内容或样式；id 和 children 只能由结构操作维护，禁止直接改写。"""

        allowed = {item.name for item in fields(Node)} - {"id", "children"}
        if patch.keys() - allowed:
            raise MindMapError("包含不可直接修改的节点字段")
        node = self.node(node_id)
        for key, value in patch.items():
            setattr(node, key, deepcopy(value))

    def remove_nodes(self, node_ids) -> None:
        """删除选中子树；不再被节点使用的图片资源在同一事务中清理，可一起撤销恢复。"""
        node_ids = list(node_ids)

        roots = self.roots(node_ids)
        if self.document.root_id in roots:
            raise MindMapError("根节点不能删除")
        parents = self.document.parents()
        for node_id in roots:
            self.node(parents[node_id]).children.remove(node_id)
            for child in self.document.descendants(node_id):
                del self.document.nodes[child]

    def move_nodes(self, node_ids, target_id: str, position: str = "inside") -> None:
        """保持先序顺序移动子树；position 支持 inside、before、after。"""
        node_ids = list(node_ids)

        roots = self.roots(node_ids)
        if not roots:
            return
        self.node(target_id)
        if self.document.root_id in roots:
            raise MindMapError("根节点不能移动")
        moving = {x for root in roots for x in self.document.descendants(root)}
        if target_id in moving:
            raise MindMapError("不能移动到自身或后代节点")
        parents = self.document.parents()
        if position not in ("inside", "before", "after"):
            raise MindMapError("不支持的移动位置")
        if position != "inside" and target_id == self.document.root_id:
            raise MindMapError("不能移动为根节点的同级")
        parent_id = target_id if position == "inside" else parents[target_id]
        for node_id in roots:
            self.node(parents[node_id]).children.remove(node_id)
        parent = self.node(parent_id)
        index = (
            len(parent.children)
            if position == "inside"
            else (parent.children.index(target_id) + (position == "after"))
        )
        parent.children[index:index] = roots
        parent.expanded = True
        if parent_id == self.document.root_id and position != "inside":
            side = self.document.branch_sides()[target_id]
            for node_id in roots:
                self.node(node_id).direction = side if side in ("left", "right") else None

    def set_expanded(self, node_id: str, expanded: bool, *, recursive: bool = False) -> None:
        """修改折叠状态；递归模式也只产生一条历史记录。"""

        ids = self.document.descendants(node_id) if recursive else [node_id]
        for current in ids:
            self.node(current).expanded = expanded

    def attach_image(self, node_id: str, data: bytes, media_type: str = "image/png", *, width=160) -> str:
        """原子导入图片资源并关联节点，相同内容只保存一份。"""

        asset = Asset.from_bytes(data, media_type)
        self.document.assets[asset.id] = asset
        node = self.node(node_id)
        node.image_id, node.image_width = asset.id, width
        return asset.id

    def copy_fragment(self, node_ids) -> MindMapDocument:
        """建立跨实例剪贴板文档，保留选中子树的层级、节点属性和所需图片资源。"""
        roots = self.roots(node_ids)
        if not roots:
            raise MindMapError("请选择需要复制的节点")
        selected = {x for root in roots for x in self.document.descendants(root)}
        fragment = MindMapDocument.new("剪贴板")
        fragment.root.children = roots.copy()
        fragment.nodes.update({x: deepcopy(self.node(x)) for x in selected})
        # 根级自动分配方向会因复制范围改变；记录原分支方向，保持复制后的节点朝向。
        sides = self.document.branch_sides()
        for root in roots:
            fragment.nodes[root].direction = sides[root] if sides[root] in ("left", "right") else None
        fragment.assets = {
            node.image_id: self.document.assets[node.image_id]
            for node in fragment.nodes.values()
            if node.image_id
        }
        fragment.layout = deepcopy(self.document.layout)
        fragment.validate()
        return fragment

    def paste_fragment(self, parent_id: str, fragment: MindMapDocument) -> list[str]:
        """插入剪贴板中的顶层子树，为全部节点重新生成 ID，并按内容哈希复用图片资源。"""
        fragment = deepcopy(fragment)

        fragment.validate()
        parent = self.node(parent_id)
        mapping = {node_id: new_id() for node_id in fragment.nodes if node_id != fragment.root_id}
        for node_id, new in mapping.items():
            node = deepcopy(fragment.nodes[node_id])
            node.id, node.children = new, [mapping[x] for x in node.children]
            self.document.nodes[new] = node
        roots = [mapping[x] for x in fragment.root.children]
        parent.children.extend(roots)
        parent.expanded = True
        self.document.assets.update(fragment.assets)
        return roots

    def prune_unused_assets(self) -> None:
        """提交事务前清理无节点引用的图片；资源差异与节点差异一起进入撤销历史。"""
        doc = self.document
        referenced = {node.image_id for node in doc.nodes.values() if node.image_id}
        doc.assets = {key: value for key, value in doc.assets.items() if key in referenced}
