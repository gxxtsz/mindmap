"""格式层声明原生读写的可选优化能力；不导入 Qt，也不持有宿主的活动编辑状态。"""

from dataclasses import fields

from mindmap_core import (
    Asset,
    FormatRegistry,
    LayoutConfig,
    MindMapDocument,
    MindMapError,
    MindMapPage,
    Node,
    Theme,
)

from .mindmap import MindmapCodec

_NODE_FIELDS = frozenset(item.name for item in fields(Node))
# 固定原始入口身份，避免类方法在运行中被替换后仍被误认成默认实现。
_REGISTRY_METHODS = {name: getattr(FormatRegistry, name) for name in ("get", "resolve", "write")}
_NATIVE_WRITE = MindmapCodec.write
_NATIVE_READ = MindmapCodec.read
_NATIVE_READ_VALIDATED = MindmapCodec.read_validated
_READ_METHODS = {name: getattr(FormatRegistry, name) for name in ("get", "resolve", "read")}


def supports_owned_load(registry: FormatRegistry) -> bool:
    """检查原生读取是否必然返回由解析器独占的新文档，供宿主转移所有权。

    只接受标准注册表和未覆写的内置 MindmapCodec.read；子类、实例或类方法替换
    一律返回 False，宿主继续通过 replace_document 建立隔离副本。此查询不读取
    文件、不跳过任何校验；True 只允许宿主接管刚读取且尚未向外发布的结果，不能
    用来接管任意现存快照。准入判断归格式实现所有，Qt 层无需访问注册表私有字段。
    """
    if type(registry) is not FormatRegistry:
        return False
    for name, method in _READ_METHODS.items():
        if getattr(getattr(registry, name), "__func__", None) is not method:
            return False
    try:
        importer = registry.get("mindmap").importer
    except MindMapError:
        return False
    return (type(importer) is MindmapCodec and getattr(importer.read, "__func__", None) is _NATIVE_READ
            and getattr(importer.read_validated, "__func__", None) is _NATIVE_READ_VALIDATED)


def supports_process_save(registry: FormatRegistry, *, format_id: str = "mindmap") -> bool:
    """只读查询当前格式能否由独立进程重建默认编码器并保存。

    当前仅支持标准注册表中的内置原生编码器；子类、替换过调度或编码方法的实例、
    单向格式及其他格式均返回 False，宿主应保留同步保存。能力判断由格式包维护，
    应用层无需检查具体类或私有字段。该接口不提交输入、不编码、不写文件，也不
    表示任意文档都可跨进程；捕获快照后还须调用 can_process_snapshot() 验证载荷。
    """
    if format_id != "mindmap" or type(registry) is not FormatRegistry:
        return False
    for name, method in _REGISTRY_METHODS.items():
        if getattr(getattr(registry, name), "__func__", None) is not method:
            return False
    try:
        entry = registry.get(format_id)
    except MindMapError:
        return False
    return (entry.can_save and type(entry.exporter) is MindmapCodec
            and getattr(entry.exporter.write, "__func__", None) is _NATIVE_WRITE)


def can_process_snapshot(document: MindMapDocument) -> bool:
    """检查捕获快照是否只包含适合跨进程传输的标准实体；不复制或修改输入。

    扩展文档、节点、资源及节点额外挂载属性返回 False，避免把 GUI 对象、本地类
    或未声明的扩展状态交给 pickle。宿主应回退原同步写入器，不能删除这些状态来
    强行启用后台保存。文档本身仍须遵循核心校验契约，此查询不替代格式编码校验。
    """
    return (type(document) is MindMapDocument and type(document.layout) is LayoutConfig
            and type(document.theme) is Theme
            and all(type(node) is Node and vars(node).keys() == _NODE_FIELDS
                    for node in document.nodes.values())
            and all(type(page) is MindMapPage and type(page.layout) is LayoutConfig and type(page.theme) is Theme
                    and vars(page).keys() == {"id", "root_id", "owner_id", "layout", "theme"}
                    for page in document.iter_pages())
            and all(type(asset) is Asset for asset in document.assets.values()))
