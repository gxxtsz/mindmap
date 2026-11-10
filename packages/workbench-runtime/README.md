# 工作台运行时

`workbench-runtime` 是基于 PySide6 的通用桌面宿主，Python 导入名为 `workbench_runtime`。运行时实现宿主服务，编辑器和文件格式由插件提供。参见[工作台插件接口](../workbench-api/README.md)。

## 主要职责

- 提供主窗口、文档标签、分屏、停靠面板和主题。
- 按依赖顺序激活插件，管理命令、面板等扩展。插件激活失败时撤销其已完成的注册，退出时释放插件资源。
- 协调文档的打开、保存和关闭，管理最近文件、操作日志和会话。恢复会话时，运行时根据保存的路径和视图状态重新打开文档，由编辑器插件读取文件内容。

## 宿主入口

通过 `WorkbenchConfig` 设置应用标识、标题和图标，通过 `WorkbenchRuntimeWindow` 创建窗口。`run_desktop()` 提供启动参数解析和 Qt 事件循环。参见[个人工作台的装配实现](../../apps/personal-workbench/src/personal_workbench/app.py)。

构造窗口时，可用 `storage_dir` 指定状态、布局和设置的存储目录，用 `restore=False` 跳过启动时的文档与布局恢复。

产品可传入 `profile_upgrade(state, settings)` 处理已加载的配置。回调在插件激活和会话恢复前修改状态及设置值字典，返回两份数据各自的变更标记。运行时将有变更且通过校验的现有配置原子写回，即使跳过会话恢复也会运行回调。

## 插件接入 API

省略 `manifests` 时，运行时从 `personal_workbench.plugins` 入口点组发现插件。传入清单列表可指定候选插件，传入空列表可单独启动桌面宿主。运行时根据设置启用插件，校验协议版本，并为每个激活的插件提供独立的 `PluginContext`。

可参考思维导图插件的[入口声明](../../plugins/workbench-plugin-mindmap/adapter/pyproject.toml)和[清单实现](../../plugins/workbench-plugin-mindmap/adapter/src/workbench_plugin_mindmap/__init__.py)。
