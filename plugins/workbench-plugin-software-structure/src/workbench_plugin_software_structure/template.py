"""保存随插件分发、可直接打开的软件结构模板。"""

from importlib.resources import files
from pathlib import Path

from PySide6.QtCore import QIODevice, QSaveFile, QStandardPaths
from PySide6.QtWidgets import QDialog, QFileDialog
from workbench_api.qt import UiService

DOWNLOAD_TEMPLATE_COMMAND_ID = "software-structure.download-template"
_SUFFIX = "software-structure"
_TEMPLATE_NAME = f"软件结构模板.{_SUFFIX}"


def download_template(ui: UiService) -> None:
    """请用户选择模板副本的保存路径，并通过宿主状态栏报告结果。

    保存对话框负责补全缺省后缀及确认覆盖。取消时直接返回，读写错误交给宿主命令统一提示和记录。
    """
    directory = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    if not directory or not Path(directory).is_dir():
        directory = str(Path.home())

    dialog = QFileDialog(ui.parent, "下载软件结构模板", directory, f"软件结构 (*.{_SUFFIX})")
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    dialog.setFileMode(QFileDialog.FileMode.AnyFile)
    dialog.setDefaultSuffix(_SUFFIX)
    dialog.selectFile(_TEMPLATE_NAME)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target = Path(dialog.selectedFiles()[0])
    finally:
        dialog.deleteLater()

    # 对话框在最终路径上确认覆盖，校验后缀后直接使用该路径进行写入。
    if target.suffix.lower() != f".{_SUFFIX}":
        raise ValueError(f"请使用 .{_SUFFIX} 扩展名保存模板")

    try:
        content = files(__package__).joinpath("resources", _TEMPLATE_NAME).read_bytes()
        _write_template(target, content)
    except OSError as error:
        raise OSError(f"保存软件结构模板失败（{target}，{error.strerror or error}）") from error
    ui.show_message(f"软件结构模板已保存到 {target}")


def _write_template(target: Path, content: bytes) -> None:
    """将资源原样写入同目录的临时文件，完整写入后再原子替换目标。"""
    output = QSaveFile(str(target))
    # 临时文件创建失败时直接报错，确保原文件始终由原子提交保护。
    output.setDirectWriteFallback(False)
    if not output.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(output.errorString())
    if output.write(content) != len(content):
        detail = output.errorString()
        output.cancelWriting()
        raise OSError(f"模板内容写入不完整（{detail}）")
    if not output.commit():
        raise OSError(output.errorString())
