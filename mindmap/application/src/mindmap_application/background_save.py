"""后台保存进程入口，不导入 Qt、不创建窗口，也不访问活跃编辑引擎。"""

import os
from typing import NamedTuple

from mindmap_core.formats import DirectoryVersion, save_directory


class SavedSnapshot(NamedTuple):
    """工作进程返回的执行标识及实际写入的目录版本，可通过进程队列传输。"""

    pid: int
    version: DirectoryVersion


def save_snapshot(document, path, *, expected_version: DirectoryVersion | None) -> SavedSnapshot:
    """在工作进程校验、编码并提交捕获的导图快照。

    工作进程只接收数据，不接收 GUI 对象或回调。expected_version 由调度时捕获，
    编码后、替换前由本进程核验。成功结果携带
    实际写入的版本，GUI 不会将后台提交后其他进程写入的内容误认为本次结果。
    """
    version = save_directory(document, path, expected_version=expected_version)
    return SavedSnapshot(os.getpid(), version)
