# 个人工作台入口

本包装配通用运行时、内置导图与文本插件，以及旧配置只读迁移。窗口不提供导图专属属性，业务能力通过插件和文档服务取得。

在仓库根目录启动：

```powershell
uv sync --locked --all-packages --group dev
uv run --locked --package personal-workbench personal-workbench
# 也可使用 Python 模块入口
uv run --locked --package personal-workbench python -m personal_workbench
```

命令末尾可追加 `.mindmap`、`.txt` 或 `.md` 文件路径。常用参数为 `--state-dir`、`--theme`、`--no-restore`、`--compact`、`--size` 和 `--screenshot`，完整用法见 `--help`。

旧应用包、命令和 Python 兼容接口已移除。默认配置目录仍为系统配置位置下的 `mindmap/mindmap-workbench`；原文件保留，只读导入历史配置，已有新版会话直接恢复。

架构、插件选项与验证流程见[工作台运行时](../../packages/workbench-runtime/README.md)，导图详细操作见[思维导图插件](../../plugins/workbench-plugin-mindmap/README.md)。
