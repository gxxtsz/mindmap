"""后台保存进程入口，不导入 Qt、不创建窗口，也不访问活跃编辑引擎。"""

import os
from pathlib import Path
from typing import NamedTuple

from mindmap_core import MindMapDocument
from mindmap_core.formats import DirectoryUpdate, DirectoryVersion, save_directory


class SavedSnapshot(NamedTuple):
    """工作进程返回的执行标识及实际写入的目录版本，可通过进程队列传输。"""

    pid: int
    version: DirectoryVersion


def save_snapshot(
    document: MindMapDocument | DirectoryUpdate, path: Path, *, expected_version: DirectoryVersion | None,
) -> SavedSnapshot:
    """在工作进程编码并提交完整导图快照或本次受影响的目录成员。

    工作进程只接收数据，不接收 GUI 对象或回调。expected_version 由调度时捕获，
    编码后、替换前由本进程核验。成功后返回实际提交的全部成员版本，供界面线程
    更新历史标记及下次增量保存基准。
    """
    version = save_directory(document, path, expected_version=expected_version)
    return SavedSnapshot(os.getpid(), version)
