"""从节点专用详情页下载软件结构导入模板。"""

from pathlib import Path

from PySide6.QtCore import QIODevice, QSaveFile, QStandardPaths
from PySide6.QtWidgets import QDialog, QFileDialog

from .document import SOFTWARE_STRUCTURE_SUFFIX, TEMPLATE_FILENAME, read_bundled_template


def _write_template_atomic(target: Path, content: bytes) -> None:
    """完整写入同目录的临时文件后提交，失败时保留现有模板文件。"""
    output = QSaveFile(str(target))
    output.setDirectWriteFallback(False)
    if not output.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(output.errorString())

    try:
        if output.write(content) != len(content):
            raise OSError(f"文件内容写入不完整（{output.errorString()}）")
    except OSError:
        # 取消后让 Qt 丢弃临时文件，短写不会破坏目标文件。
        output.cancelWriting()
        output.commit()
        raise

    if not output.commit():
        raise OSError(output.errorString())


def download_template(parent, report) -> None:
    """请用户选择模板副本的保存路径，并通过宿主状态栏报告结果。

    保存对话框负责补全缺省后缀及确认覆盖。取消时直接返回，读写错误交给宿主命令统一提示和记录。
    """
    directory = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    if not directory or not Path(directory).is_dir():
        directory = str(Path.home())

    file_filter = f"软件结构 (*.{SOFTWARE_STRUCTURE_SUFFIX})"
    dialog = QFileDialog(parent, "下载软件结构模板", directory, file_filter)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    dialog.setFileMode(QFileDialog.FileMode.AnyFile)
    dialog.setDefaultSuffix(SOFTWARE_STRUCTURE_SUFFIX)
    dialog.selectFile(TEMPLATE_FILENAME)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target = Path(dialog.selectedFiles()[0])
    finally:
        dialog.deleteLater()

    # 对话框在最终路径上确认覆盖，校验后缀后直接使用该路径进行写入。
    if target.suffix.lower() != f".{SOFTWARE_STRUCTURE_SUFFIX}":
        raise ValueError(f"请使用 .{SOFTWARE_STRUCTURE_SUFFIX} 扩展名保存模板")

    try:
        _write_template_atomic(target, read_bundled_template())
    except OSError as error:
        raise OSError(f"保存软件结构模板失败（{target}，{error.strerror or error}）") from error
    report(f"软件结构模板已保存到 {target}")
