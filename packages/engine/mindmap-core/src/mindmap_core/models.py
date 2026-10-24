"""文档模型与完整性检查。此模块只使用标准库，不接触 Qt 或文件系统。"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .branch_styles import BranchStyle
from .config import LayoutConfig
from .errors import MindMapError


def new_id() -> str:
    """生成不依赖节点内容的稳定标识，复制节点时必须重新生成。"""
    return uuid4().hex


@dataclass
class Theme:
    """画布主题；颜色使用 Qt/SVG 均可识别的十六进制字符串。"""

    name: str = "浅色"
    background: str = "#f5f6f8"
    node_background: str = "#ffffff"
    text: str = "#253044"
    border: str = "#c5ccd7"
    accent: str = "#3478d5"
    palette: list[str] = field(default_factory=lambda: ["#3578cc", "#289b79", "#b076c2", "#cf8b38"])

    @classmethod
    def dark(cls) -> Theme:
        """返回独立的深色主题实例，修改它不会影响其他文档。"""
        return cls(
            "深色",
            "#20242c",
            "#2d3340",
            "#e6eaf0",
            "#596476",
            "#79b3ff",
            ["#79b3ff", "#6ac9a8", "#cba4ed", "#efbd78"],
        )


@dataclass
class Node:
    """节点实体。

    children 按顺序存放子节点 ID，父引用由文档临时索引推导，避免循环序列化。
    style 支持字体、颜色、宽度等原生绘制属性；metadata 只能包含 JSON 数据。
    """

    text: str = "新节点"
    id: str = field(default_factory=new_id)
    children: list[str] = field(default_factory=list)
    expanded: bool = True
    direction: str | None = None
    text_format: str = "plain"
    style: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    icons: list[str] = field(default_factory=list)
    note: str = ""
    hyperlink: str = ""
    image_id: str | None = None
    image_width: float = 160.0
    metadata: dict[str, Any] = field(default_factory=dict)
    submap_id: str | None = None
    # 详情页与子导图分别归属当前节点；详情页不要求入口是叶子节点。
    detail_page_id: str | None = None

    @property
    def is_image_node(self) -> bool:
        """只读判断当前实体是否为纯图片节点，返回布尔值。

        无参数；仅识别元数据中的 ``image_only: true``，普通图片附件及旧文件
        均返回 False。本查询不修改实体、不解码图片、无正常业务异常，也不依赖
        GUI 线程；实体由所属文档管理，外部应通过引擎取得快照并避免直接修改。
        资源存在、正文为空及禁止附属页面的约束由文档校验负责。
        """
        return isinstance(self.metadata, dict) and self.metadata.get("image_only") is True

    def __deepcopy__(self, memo: dict) -> Node:
        """建立隔离节点副本，跳过不可变标量的通用序列化重建开销。

        文字、标识和数值可直接共享；子节点、样式、标签和元数据仍通过同一个
        memo 深拷贝，保留嵌套容器的别名关系和循环处理。遍历实例字段而非手写
        固定字段列表，使后续新增字段及子类扩展也能进入事务快照和撤销历史。
        """
        result = type(self).__new__(type(self))
        memo[id(self)] = result
        state = memo.get(id(self.__dict__))
        if state is not None:
            result.__dict__ = state
        else:
            memo[id(self.__dict__)] = result.__dict__
            atomic = (str, int, float, bool, bytes, type(None))
            result.__dict__.update(
                (name, value if type(value) in atomic else deepcopy(value, memo))
                for name, value in self.__dict__.items()
            )
        if type(self) is not Node:
            # 普通节点没有 slots；扩展节点若声明了槽字段，同样保持原有深拷贝能力。
            for base in type(self).__mro__:
                slots = base.__dict__.get("__slots__", ())
                for name in (slots,) if isinstance(slots, str) else slots:
                    if name in ("__dict__", "__weakref__"):
                        continue
                    if name.startswith("__") and not name.endswith("__"):
                        name = f"_{base.__name__.lstrip('_')}{name}"
                    if hasattr(self, name):
                        object.__setattr__(result, name, deepcopy(getattr(self, name), memo))
        return result


@dataclass(frozen=True)
class Asset:
    """不可变图片资源。内容哈希用于去重，二进制数据不参与历史深拷贝。"""

    id: str
    data: bytes
    media_type: str = "image/png"

    @classmethod
    def from_bytes(cls, data: bytes, media_type: str = "image/png") -> Asset:
        """从图片字节建立资源；图像能否解码由图形适配层进一步检查。"""
        if not isinstance(data, bytes) or not data:
            raise MindMapError("图片内容不能为空")
        return cls(hashlib.sha256(data).hexdigest(), data, media_type)

    def __deepcopy__(self, memo: dict) -> Asset:
        return self


@dataclass
class MindMapPage:
    """子导图与详情页共用的独立页面记录。

    页面只保存根节点、入口和外观配置；节点与资源统一放在文档表中。
    页面之间仅通过字符串 ID 关联，因此多级子导图不会形成递归对象；详情页不再嵌套页面。
    """

    id: str
    root_id: str
    owner_id: str
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    theme: Theme = field(default_factory=Theme)


@dataclass(frozen=True)
class PathEntry:
    """可点击路径项；区域路径的 node_id 可为空，标题只用于显示，不作为定位依据。"""

    page_id: str | None
    node_id: str | None
    text: str
    page_root: bool = False


@dataclass
class InspirationArea:
    """文档内无入口节点的独立灵感区，坐标均为导图逻辑像素。

    root_ids 保存游离树的稳定顺序，positions 仅保存这些根的左上角坐标；
    普通后代仍由树布局决定。固定默认区域标识使旧文件的空区也能恢复视图。
    所有字段均由引擎事务管理，外部不得直接修改借用记录。
    """

    id: str = "@inspiration"
    root_ids: list[str] = field(default_factory=list)
    positions: dict[str, tuple[float, float]] = field(default_factory=dict)
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    theme: Theme = field(default_factory=Theme)


@dataclass
class MindMapDocument:
    """完整文档。节点树以根标识和邻接表表达，支持深层树的非递归读写。"""

    root_id: str
    nodes: dict[str, Node]
    assets: dict[str, Asset] = field(default_factory=dict)
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    theme: Theme = field(default_factory=Theme)
    metadata: dict[str, Any] = field(default_factory=dict)
    submaps: dict[str, MindMapPage] = field(default_factory=dict)
    detail_pages: dict[str, MindMapPage] = field(default_factory=dict)
    inspiration: InspirationArea = field(default_factory=InspirationArea)

    @classmethod
    def new(cls, text: str = "中心主题") -> MindMapDocument:
        """建立默认向右展开、使用圆角正交连接线的新文档。

        初始样式显式写入根主题，后续新增主题通过既有的父主题复制规则沿用。
        不改变 Node 的空样式语义，文件读取时原有的未配置曲线仍可原样恢复；
        初始化也不经过引擎事务，因此新文档保持未修改状态且没有额外撤销记录。
        """
        root = Node(text, style={"branch": {"kind": BranchStyle.ROUND.value}})
        return cls(root.id, {root.id: root})

    @property
    def root(self) -> Node:
        return self.nodes[self.root_id]

    @property
    def root_ids(self) -> tuple[str, ...]:
        """返回当前读模型的全部普通树根；传统单根文档保持一个元素。"""
        return (self.root_id,)

    def ordered_nodes(self) -> list[str]:
        """按根顺序及各树先序返回页内节点，不跨越附属页面边界。"""
        return [key for root in self.root_ids for key in self.descendants(root)]

    def parents(self) -> dict[str, str]:
        """计算临时父索引；它不属于持久化内容。"""
        return {child: node.id for node in self.nodes.values() for child in node.children}

    def descendants(self, node_id: str) -> list[str]:
        """按先序返回节点和全部后代，使用显式栈避免深层树的 Python 递归限制。"""
        result, stack = [], [node_id]
        while stack:
            current = stack.pop()
            result.append(current)
            stack.extend(reversed(self.nodes[current].children))
        return result

    def branch_sides(self) -> dict[str, str]:
        """推导一级分支方向；双向模式优先保留指定方向，再按分支数量平衡。"""
        result: dict[str, str] = {}
        counts = {"left": 0, "right": 0}
        for child in self.root.children:
            side = self.layout.direction
            if side == "both":
                side = self.nodes[child].direction or min(counts, key=counts.get)
                counts[side] += 1
            for node_id in self.descendants(child):
                result[node_id] = side
        result[self.root_id] = self.layout.direction if self.layout.direction != "both" else "right"
        return result

    def page_record(self, page_id: str | None = None) -> MindMapDocument | MindMapPage | InspirationArea:
        """按稳定标识借用主导图、附属页面或灵感区记录。

        None 表示主导图，inspiration.id 表示多根灵感区，其余标识查询附属页；
        目标不存在抛出 KeyError，不创建默认页面、不修改文档或历史。
        返回值仍属于当前文档，外部不得直接修改；修改配置应使用引擎事务接口。
        查询只做字典访问，不合并页面表，避免局部编辑退回全文件扫描。
        """
        if page_id is None:
            return self
        if page_id == self.inspiration.id:
            return self.inspiration
        if page_id in self.submaps:
            return self.submaps[page_id]
        return self.detail_pages[page_id]

    def page_kind(self, page_id: str | None = None) -> str:
        """只读返回 main、submap、detail 或 inspiration，无效标识抛出 KeyError。"""
        if page_id is None:
            return "main"
        if page_id == self.inspiration.id:
            return "inspiration"
        if page_id in self.submaps:
            return "submap"
        if page_id in self.detail_pages:
            return "detail"
        raise KeyError(page_id)

    def has_page(self, page_id: str | None) -> bool:
        """检查页面是否仍存在，供撤销删除后的导航回退使用。"""
        return (page_id is None or page_id == self.inspiration.id
                or page_id in self.submaps or page_id in self.detail_pages)

    def iter_pages(self):
        """依次借用两类附属页面；只用于完整装载、校验和显式全文件操作。"""
        yield from self.submaps.values()
        yield from self.detail_pages.values()

    def page_view(self, page_id: str | None = None, *,
                  inspiration_root_id: str | None = None) -> MindMapPageView:
        """借用一页的节点与配置，供布局、渲染和面板只读使用。

        返回对象不拥有整份文档，不能作为文件保存；调用方不得修改借用的实体。
        主导图使用 None 标识。两类页面引用保留在节点上，页内校验不跨页展开。
        灵感区标识返回 MindMapForestView，允许没有任何节点，应通过 root_ids
        枚举普通树；附属页面不混入读模型。目标无效时抛出 KeyError。

        inspiration_root_id 仅允许用于灵感区，指定时只借用该游离根及普通后代，
        保留 inspiration 类别，并以独立的区域记录将展示根置于 (0, 0)。原文档的
        根顺序、坐标及实体均不修改；省略时保持完整总览。无效根或用于其他页面
        时抛出 KeyError。该查询不产生历史，结果不能用于整份文件保存。
        """
        page = self.page_record(page_id)
        if page_id == self.inspiration.id:
            if inspiration_root_id is not None:
                if inspiration_root_id not in page.root_ids:
                    raise KeyError(inspiration_root_id)
                page = InspirationArea(page.id, [inspiration_root_id],
                                       {inspiration_root_id: (0.0, 0.0)}, page.layout, page.theme)
            nodes = {key: self.nodes[key] for root in page.root_ids for key in self.descendants(root)}
            assets = {n.image_id: self.assets[n.image_id] for n in nodes.values() if n.image_id}
            return MindMapForestView(page, nodes, assets, self.metadata)
        if inspiration_root_id is not None:
            raise KeyError(inspiration_root_id)
        nodes = {key: self.nodes[key] for key in self.descendants(page.root_id)}
        assets = {node.image_id: self.assets[node.image_id] for node in nodes.values() if node.image_id}
        view = MindMapPageView(page.root_id, nodes, assets, page.layout, page.theme, self.metadata)
        # 页面类别只属于读模型上下文，不是文件字段；详情页单页校验仍执行嵌套禁令。
        view.kind = self.page_kind(page_id)
        return view

    def owned_descendants(self, node_id: str) -> list[str]:
        """迭代取得普通分支及其拥有的两类页面节点，用于删除和完整复制。"""
        result, stack = [], [node_id]
        while stack:
            key = stack.pop()
            node = self.nodes[key]
            result.append(key)
            stack.extend(reversed(node.children))
            if node.submap_id:
                stack.append(self.submaps[node.submap_id].root_id)
            if node.detail_page_id:
                stack.append(self.detail_pages[node.detail_page_id].root_id)
        return result

    def validate(self) -> None:
        """验证完整文档；规则由独立校验模块维护，失败时不改变当前对象。"""
        from .validation import validate_document

        validate_document(self)


class MindMapPageView(MindMapDocument):
    """具有既有布局文档接口的单页读模型；外部页面引用由完整文档负责校验。"""

    kind = "main"

    def validate(self) -> None:
        from .validation import validate_document

        validate_document(self, page_only=True)


class MindMapForestView(MindMapPageView):
    """借用多棵普通树的灵感区读模型，禁止作为完整文档保存。

    root_ids 是正式的多根接口；root_id 仅为既有单根调用提供首根或空字符串。
    空区没有伪造的节点，调用方应使用 root_ids/nodes 判断是否可操作主题。
    """

    kind = "inspiration"

    def __init__(self, area, nodes, assets, metadata):
        super().__init__(area.root_ids[0] if area.root_ids else "", nodes, assets,
                         area.layout, area.theme, metadata, inspiration=area)

    @property
    def root_ids(self) -> tuple[str, ...]:
        """返回独立根标识的不可变快照；空区返回空元组，不产生修改。"""
        return tuple(self.inspiration.root_ids)

    def tree_view(self, root_id):
        """借用一棵游离树作为传统单根布局输入，不复制节点及图片字节。"""
        nodes = {key: self.nodes[key] for key in self.descendants(root_id)}
        return MindMapPageView(root_id, nodes, self.assets, self.layout, self.theme, self.metadata)

    def page_view(self, page_id=None, *, inspiration_root_id=None):
        """多根读模型已限定当前区域，返回自身供候选校验，不丢弃其余游离树。"""
        if page_id is None or page_id == self.inspiration.id:
            if inspiration_root_id is not None:
                return super().page_view(self.inspiration.id, inspiration_root_id=inspiration_root_id)
            return self
        raise KeyError(page_id)

    def branch_sides(self):
        """逐树解析方向，根坐标不参与树内分支方向计算。"""
        return {key: side for root in self.root_ids for key, side in self.tree_view(root).branch_sides().items()}
