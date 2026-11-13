"""保存随插件分发、可直接打开的软件结构模板。"""

from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QDialog, QFileDialog
from workbench_api.qt import UiService

from .document import SOFTWARE_STRUCTURE_SUFFIX, TEMPLATE_FILENAME, read_bundled_template
from .storage import write_atomic

DOWNLOAD_TEMPLATE_COMMAND_ID = "software-structure.download-template"


def download_template(ui: UiService) -> None:
    """请用户选择模板副本的保存路径，并通过宿主状态栏报告结果。

    保存对话框负责补全缺省后缀及确认覆盖。取消时直接返回，读写错误交给宿主命令统一提示和记录。
    """
    directory = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    if not directory or not Path(directory).is_dir():
        directory = str(Path.home())

    file_filter = f"软件结构 (*.{SOFTWARE_STRUCTURE_SUFFIX})"
    dialog = QFileDialog(ui.parent, "下载软件结构模板", directory, file_filter)
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
        write_atomic(target, read_bundled_template())
    except OSError as error:
        raise OSError(f"保存软件结构模板失败（{target}，{error.strerror or error}）") from error
    ui.show_message(f"软件结构模板已保存到 {target}")
