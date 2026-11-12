"""原子写入软件结构文件和模板副本。"""

from pathlib import Path

from PySide6.QtCore import QIODevice, QSaveFile


def write_atomic(target: Path, content: bytes, *, expected: bytes | None = None) -> None:
    """完整写入同目录的临时文件后提交。提供快照时，在提交前检查原文件是否仍与快照一致。"""
    output = QSaveFile(str(target))
    output.setDirectWriteFallback(False)
    if not output.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(output.errorString())

    try:
        if output.write(content) != len(content):
            raise OSError(f"文件内容写入不完整（{output.errorString()}）")
        # 尽量靠近原子替换时读取原文件，避免保存筛选时覆盖外部已提交的修改。
        if expected is not None and target.read_bytes() != expected:
            raise OSError("文件已在外部修改，请关闭该文件的全部标签后重新打开")
    except OSError:
        # 取消后让 Qt 丢弃临时文件，短写和外部修改走同一回滚路径。
        output.cancelWriting()
        output.commit()
        raise

    if not output.commit():
        raise OSError(output.errorString())
