"""后台保存进程入口，不导入 Qt、不创建窗口，也不访问活跃编辑引擎。"""

import os
from typing import NamedTuple

from mindmap_core.formats import save_document
from mindmap_core.formats.files import FileVersion


class SavedSnapshot(NamedTuple):
    """工作进程返回的执行标识及实际写入的文件版本，可通过进程队列传输。"""

    pid: int
    version: FileVersion | None


def save_snapshot(document, path, *, expected_version: FileVersion | None) -> SavedSnapshot:
    """使用独立原生编解码器完整校验、编码并原子写入捕获的稳定快照。

    调用者只为已确认的内置原生格式使用此入口。自定义写入器和编码器仍在宿主
    原同步边界执行。工作进程只接收数据，不接收 GUI 对象、回调或共享注册表。
    expected_version 由调度时捕获，编码后、替换前由本进程核验。成功结果携带
    实际写入的版本，GUI 不会将后台提交后其他进程写入的内容误认为本次结果。
    """
    version = save_document(document, path, format_id="mindmap", expected_version=expected_version)
    return SavedSnapshot(os.getpid(), version)
