"""后台保存进程入口，不导入 Qt、不创建窗口，也不访问活跃编辑引擎。"""

import os

from mindmap_formats import save_document


def save_snapshot(document, path):
    """使用独立原生编解码器完整校验、编码并原子写入捕获的稳定快照。

    调用者只为已确认的内置原生格式使用此入口。自定义写入器和编码器仍在宿主
    原同步边界执行。工作进程只接收数据，不接收 GUI 对象、回调或共享注册表。
    返回实际执行写入的进程标识，便于验收后台任务确实与 GUI 分离。
    """
    save_document(document, path, format_id="mindmap")
    return os.getpid()
