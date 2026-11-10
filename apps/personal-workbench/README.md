# 个人工作台应用

`personal-workbench` 是桌面应用入口，负责设置应用名称和图标、装配通用运行时，并接入历史配置迁移。参见[工作台运行时](../../packages/workbench-runtime/README.md)。

应用默认启用思维导图插件，支持 `.mindmap` 文件。可在“设置 → 插件管理”中调整启用状态，重启后生效。详见[思维导图插件](../../plugins/workbench-plugin-mindmap/README.md)。

## 启动

在仓库根目录执行以下命令。

```powershell
uv sync --locked --all-packages --group dev
uv run --locked --package personal-workbench personal-workbench
```

也可运行 `uv run --locked --package personal-workbench python -m personal_workbench`。命令末尾可附加一个待打开的文件路径。追加 `--help` 可查看主题、配置目录和会话恢复等选项。

## 配置兼容

默认配置位于系统配置目录下的 `mindmap/mindmap-workbench`。通过 `--state-dir` 指定目录时，该目录用于读写配置和查找历史文件。

- `workbench-state-v1.json` 保存最近文件和插件会话状态。运行期间合并连续操作后，将打开的标签、活动文档、分屏布局和视图状态一起原子写入。正常退出也会提交一次，异常退出后恢复最近成功写入的工作现场。
- 首次迁移可读取旧的 `workbench-layout-v1.json`。主状态文件缺失时，应用还会尝试从同目录的 `documents-v1.json` 和 `layouts-v1.json` 导入。
- `workbench-settings-v1.json` 保存窗口和插件的设置。文件缺失时，应用尝试从同目录的 `preferences.ini` 导入。

历史文件只读导入。启动升级在插件激活和会话恢复前清理工作台配置中已退役文本编辑器的设置、标签和相关最近文件记录。使用 `--no-restore` 时也会执行启动升级。
