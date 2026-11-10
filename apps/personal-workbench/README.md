# 个人工作台应用

`personal-workbench` 是桌面应用入口，负责设置应用名称和图标、装配通用运行时，并接入历史配置迁移。参见[工作台运行时](../../packages/workbench-runtime/README.md)。

应用默认启用思维导图和纯文本编辑插件，分别支持 `.mindmap` 和 `.txt`、`.md` 文件。可在“设置 → 插件管理”中调整启用状态，重启后生效。详见[思维导图插件](../../plugins/workbench-plugin-mindmap/README.md)和[文本编辑插件](../../plugins/workbench-plugin-text/README.md)。

## 启动

在仓库根目录执行以下命令。

```powershell
uv sync --locked --all-packages --group dev
uv run --locked --package personal-workbench personal-workbench
```

也可运行 `uv run --locked --package personal-workbench python -m personal_workbench`。命令末尾可附加一个待打开的文件路径。追加 `--help` 可查看主题、配置目录和会话恢复等选项。

## 配置兼容

默认配置位于系统配置目录下的 `mindmap/mindmap-workbench`。新版状态或设置文件缺失时，应用从对应的历史配置中只读导入。
