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

### 只读文档

仅打开既有文件的提供者将 `EditorProvider.new_document` 设为 `None`。打开对话框根据 `extensions` 列出该格式。该类型的文档处于活动状态时，文件菜单省略新建项并停用新建快捷键。欢迎页按提供了新建回调的类型显示按钮。

只读文档使用 `SavePolicy.READ_ONLY`。宿主禁用其保存和另存为动作，批量保存跳过只读文档。只读视图仍通过常规接口参与标签关闭、复制、最近文件和会话恢复。

### 视图状态

新视图默认进入活动分屏。登记时可传入 `after`，让复制视图直接插在源标签之后。编辑器在切页、平移或缩放后调用 `context.documents.view_state_changed(view.id)`，工作台合并连续通知并读取 `EditorView.capture_state()`，与标签和布局一起保存。视图快照返回可序列化数据，读取时保留输入状态和共享文档内容。

### 主菜单扩展

使用 `workbench_api.qt.MenuLocation` 指定位置。工作台决定分区标题和顺序，插件提供该分区的动作及可用状态。

| 位置 | 放置规则 |
| --- | --- |
| `FILE`（`file`） | 「文件」菜单的扩展区，位于「退出」前 |
| `FILE_EXPORT`（`file.export`） | 「文件 → 导出当前页」，有导出动作时显示 |
| `EDIT`（`edit`） | 「编辑」菜单中六个标准编辑动作之后 |
| `VIEW`（`view`） | 「视图」菜单中的业务动作区 |
| `SETTINGS`（`settings`） | 「设置」菜单的扩展区 |

- 常驻命令通过 `context.commands.register(CommandSpec(..., menu=MenuLocation.VIEW))` 注册。空字符串表示该命令无需菜单入口。
- 需要按当前文档生成动作时，使用 `context.ui.contribute_menu(MenuContribution(...))`。填充回调收到本次贡献的临时菜单，可调用 `addAction()` 或 `addMenu(标题)`，也可复用视图持有的动作。回调成功后工作台合入动作，异常会记录到操作日志。
- 编辑器通过 `edit_actions` 提供标准编辑能力。撤销、重做、剪切、复制、粘贴、全选的菜单位置及快捷键由工作台管理。
- 原有的「文件」「编辑」「视图」「设置」名称仍可作为位置别名。未知位置在注册时报告错误。旧视图的 `menu_actions` 仍可提供额外动作，新插件通过菜单贡献接口接入。
