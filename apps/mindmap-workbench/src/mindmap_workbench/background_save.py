"""后台保存进程入口，不导入 Qt、不创建窗口，也不访问活跃编辑引擎。"""

import os
from dataclasses import fields

from mindmap_core import Asset, LayoutConfig, MindMapDocument, Node, Theme
from mindmap_formats import save_document

_NODE_FIELDS = frozenset(item.name for item in fields(Node))


def can_process_snapshot(document):
    """仅把标准数据模型交给进程，扩展实体及额外挂载属性保留原同步保存契约。

    原生格式不保存节点动态属性，直接 pickle 扩展对象却可能触及 GUI 对象或
    本地类。这里明确拒绝这类后台载荷，不改变公共文档或丢弃扩展的数据。
    """
    return (type(document) is MindMapDocument and type(document.layout) is LayoutConfig
            and type(document.theme) is Theme
            and all(type(node) is Node and node.__dict__.keys() == _NODE_FIELDS
                    for node in document.nodes.values())
            and all(type(asset) is Asset for asset in document.assets.values()))


def save_snapshot(document, path):
    """使用独立原生编解码器完整校验、编码并原子写入捕获的稳定快照。

    调用者只为已确认的内置原生格式使用此入口。自定义写入器和编码器仍在宿主
    原同步边界执行。工作进程只接收数据，不接收 GUI 对象、回调或共享注册表。
    返回实际执行写入的进程标识，便于验收后台任务确实与 GUI 分离。
    """
    save_document(document, path, format_id="mindmap")
    return os.getpid()
