# 工作台插件接口

`workbench-api` 定义宿主和插件共用的公开协议，Python 导入名为 `workbench_api`。当前插件协议版本为 `API_VERSION = 2`。

## 接口范围

- `PluginManifest` 声明插件标识、版本、依赖和激活函数。`PluginContext` 为插件提供编辑器注册、文档、设置、会话状态和日志等服务。
- `EditorProvider` 声明支持的扩展名以及新建、打开操作。`DocumentHandle` 表示可由多个标签共享的文档，`EditorView` 表示每个标签独立的视图。
- `workbench_api.qt` 定义命令、停靠面板、菜单和状态栏的扩展声明，并提供主题查询与界面事件订阅的接口。

完整类型和方法见[核心协议](src/workbench_api/contracts.py)和 [Qt 扩展协议](src/workbench_api/qt.py)。

## 插件接入

插件在 `pyproject.toml` 的 `personal_workbench.plugins` 入口点组中公开清单，入口名称须与 `PluginManifest.id` 一致。

宿主发现插件时会导入清单所在的模块。将业务模块的导入和对象创建放在清单的 `activate(context)` 中，使宿主可以轻量发现插件。激活时通过上下文注册扩展，并返回实现 `dispose()` 的实例。参见[运行时的插件接入说明](../workbench-runtime/README.md#插件接入-api)。
