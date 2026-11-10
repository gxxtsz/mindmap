# workbench-runtime 工作台运行时

`workbench-runtime` 是可独立装配的 PySide6 工作台宿主，Python 导入名为 `workbench_runtime`。它提供桌面外壳和停靠布局，并协调插件、文档、命令与会话。安装运行时即可启动空工作台，安装并启用编辑器插件后才具备相应的文件编辑能力。

## 模块职责

| 层级 | 职责 |
| --- | --- |
| `workbench_runtime.ui.shell` 和 `workbench_runtime.ui.docking` | 提供主窗口、标签、分屏、停靠面板和局部主题，生成并应用布局快照。 |
| `workbench-runtime` | 装配窗口，发现并激活插件，协调文档标签、编辑命令、保存与关闭，管理欢迎页、最近文件、操作日志和会话恢复。 |
| `workbench_runtime.storage` | 提供宿主与产品迁移共用的 JSON 原子存储。 |
| `workbench-api` | 定义插件清单、上下文服务、文档与视图等接入协议。 |
| 编辑器插件 | 实现文件格式、编码、文档内容、编辑控件和实际读写，并提供可恢复的视图状态。 |
| 产品入口 | 选择应用标识、显示名称、插件配置和配置目录。已有历史数据的产品还负责提供迁移函数。 |

## 快速开始

本包支持 Python 3.12、3.13，依赖 `workbench-api` 和 PySide6 `>=6.12,<6.13`。下方示例另外需要 `workbench-plugin-text`，它提供 `.txt` 和 `.md` 纯文本编辑。详见[文本插件](../../plugins/workbench-plugin-text/README.md)。

在仓库根目录同步 uv 工作区。

```powershell
uv sync --locked --all-packages --group dev
```

将下方代码保存为仓库根目录下的 `运行文本工作台.py`。窗口构造和界面调用均在 Qt GUI 线程进行，宿主应用负责创建 `QApplication` 并运行事件循环。

```python
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication
from workbench_plugin_text import manifest as text_manifest
from workbench_runtime import WorkbenchConfig, WorkbenchRuntimeWindow


def main(path: Path | None = None, *, storage_dir: Path | None = None) -> int:
    """启动文本工作台。path 指定待打开文件，storage_dir 可隔离本次使用的配置。"""
    app = QApplication([])
    config = WorkbenchConfig(application_id="example-text-workbench", title="文本工作台")
    # 显式选中文本插件，避免同时加载当前环境中安装的其他插件。
    window = WorkbenchRuntimeWindow(config=config, storage_dir=storage_dir, manifests=[text_manifest])
    try:
        window.show()
        if path is not None:
            # 将打开异常转换为窗口状态提示，用户仍可在界面中打开其他文件。
            window.call(lambda: window.open_path(path))
        return app.exec()
    finally:
        # 正常关闭已释放窗口资源。事件循环从其他入口退出时也能完成清理。
        window.dispose()


if __name__ == "__main__":
    initial_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    raise SystemExit(main(initial_path))
```

```powershell
uv run --locked --no-sync python .\运行文本工作台.py
uv run --locked --no-sync python .\运行文本工作台.py .\README.md
```

首次启动可从欢迎页新建或打开文件。省略 `manifests` 或传入 `None` 会自动发现已安装插件，传入清单序列只从该序列选择候选插件，传入 `[]` 则启动保留欢迎页、设置和操作日志的空工作台。候选插件的启用状态仍由配置决定。

## 宿主 API

从 `workbench_runtime` 导入 `WorkbenchConfig` 和 `WorkbenchRuntimeWindow` 装配工作台。产品入口可使用同包导出的 `run_desktop()` 处理 Qt 启动流程。窗口继承 `WorkbenchWindow`，可复用外壳的窗口接口。详见[桌面外壳](docs/桌面外壳.md)。

### WorkbenchConfig

`WorkbenchConfig` 是不可变数据类。构造时校验 `application_id` 和 `title`，校验失败抛出 `ValueError`。

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `application_id` | `"workbench"` | 稳定的应用标识，用于区分布局归属和计算默认配置目录。长度为 1 至 100 个字符，首字符为 ASCII 字母或数字，其余可使用 ASCII 字母、数字、点、下划线和连字符。 |
| `title` | `"工作台"` | 欢迎页及窗口标题使用的显示名称，去除首尾空白后须有内容。 |
| `icon` | `"document"` | 窗口使用的主题图标标识。 |

`default_storage_dir() -> Path` 返回 Qt 的 `QStandardPaths.StandardLocation.GenericConfigLocation` 下以 `application_id` 命名的路径。计算路径本身只读取位置，目录由首次写入创建。

### WorkbenchRuntimeWindow 构造参数

```text
WorkbenchRuntimeWindow(
    *, config=None, storage_dir=None, theme=None, restore=True,
    manifests=None, migration=None, plugin_options=None
)
```

| 参数 | 用途 |
| --- | --- |
| `config` | 应用标识、标题和图标。省略时使用 `WorkbenchConfig()`。 |
| `storage_dir` | 字符串或 `Path`。显式路径优先于 `config.default_storage_dir()`，适用于隔离测试和沿用已有配置目录。 |
| `theme` | 接受 `"light"` 或 `"dark"`。显式主题优先于已保存的主题，未知主题名称会引发 `ValueError`。默认从浅色开始，成功恢复的布局可以带回上次主题。 |
| `restore` | 默认恢复上次的文档标签和停靠布局。设为 `False` 可从初始布局启动，仍读取设置和最近文件。 |
| `manifests` | 插件清单序列或 `None`，控制本次启动的插件发现范围。 |
| `migration` | 可选的 `migration(directory: Path)` 回调。构造时调用，返回 `(initial_state, initial_settings)` 供状态和设置加载使用。 |
| `plugin_options` | 以插件 ID 为键的字典。每个插件通过 `context.options` 取得对应字典，缺省为 `{}`，由插件解释选项。 |

### 文档操作

`path` 参数可传文件路径字符串或 `Path`。新建、打开和保存等方法以布尔值表示是否成功，插件侧保留更细的 `OperationResult`。用户也可把编辑器支持的本地文件拖入窗口。

| 方法 | 返回值与行为 |
| --- | --- |
| `provider_for(path)` | 按路径最后一个扩展名的小写形式匹配已注册的编辑器，返回提供者 ID。没有匹配项时返回 `None`。 |
| `new_document(path=None, *, provider_id=None)` | 显式提供者优先，其次按带扩展名的路径选择。省略提供者且路径无扩展名时，优先使用活动视图对应的提供者 ID，再取首个已注册提供者。新建成功返回 `True`，提供者不可用、取消或返回失败时返回 `False`，提供者直接抛出的异常会向调用方传播。文本插件将路径用作首次保存的建议路径。 |
| `open_path(path)` | 打开文件或激活已打开的同路径标签，成功返回 `True`，提供者取消时返回 `False`。没有可用编辑器时抛出 `ValueError`。提供者返回失败时抛出原始异常，只有失败消息时使用 `ValueError`。 |
| `open_dialog()` | 根据当前提供者生成文件过滤器，再调用 `open_path()`。取消文件选择返回 `False`，打开结果及异常沿用 `open_path()`。 |
| `save(*, save_as=False)` | 保存活动文档。传入 `save_as=True` 发起另存为。保存成功返回 `True`，缺少活动文档、取消或失败时返回 `False`。 |
| `save_all()` | 按标签布局顺序保存，每份共享文档只保存一次，跳过恢复占位标签。全部完成返回 `True`，遇到取消或失败时激活对应标签、停止批量操作并返回 `False`。 |
| `open_file_directory(session_id=None)` | 用系统文件管理器打开指定视图的文件所在目录，默认使用活动视图。成功返回 `True`。视图缺失、尚无路径或打开目录失败时返回 `False`，打开失败会显示状态提示并写入日志。 |

### 界面动作与生命周期

| 方法 | 返回值与行为 |
| --- | --- |
| `call(callback)` | 执行无参回调并返回其结果。捕获 `Exception` 时显示状态提示、记录错误并返回 `None`。打开失败已通过 `reported=True` 标记时沿用插件的错误日志。界面回调可通过此方法调用会抛出异常的文档操作。 |
| `action(identifier)` | 返回可在菜单或按钮中复用的 `QAction`。标识未注册时抛出 `KeyError`。 |
| `show_plugin_manager()` | 打开插件管理对话框并在关闭后返回 `None`。可查看当前状态和错误，保存的启用选择在下一次启动生效。 |
| `show()` 和 `close()` | `show()` 显示窗口，返回 `None`。正常退出调用 `close()`，通过关闭检查后提交会话并释放资源。窗口接受关闭时返回 `True`，拒绝时返回 `False`。 |
| `dispose()` | 幂等地释放事件监听、停靠管理器、文档视图和插件，返回 `None`。供测试或事件循环结束时清理使用。停靠管理器关闭时尝试保存布局，文档视图直接释放。需要保存决策和退出文档快照时应先走 `close()`。 |

常用动作标识如下。

- 文件操作为 `app.new`、`app.open`、`app.save`、`app.save_as`、`app.save_all`、`app.open_directory` 和 `app.quit`。
- 编辑操作为 `app.undo`、`app.redo`、`app.cut`、`app.copy`、`app.paste` 和 `app.select_all`。触发时重新读取焦点，优先交给窗口内有焦点的 `QLineEdit`、`QPlainTextEdit` 或 `QTextEdit`，其他情况下交给活动视图中对应且可用的 `edit_actions`。
- `tool:operation_log` 对应“视图”菜单中的“操作日志”，快捷键为 `Alt+5`。

### 最近文件与操作日志

| 方法 | 返回值与行为 |
| --- | --- |
| `remember_path(path)` | 将绝对路径置于最近列表首位，按规范化路径去重，最多保留 20 条。返回 `None`。写入失败会显示提示并记录警告。 |
| `clear_recent_files()` | 清空最近文件列表并保存，返回 `None`。 |
| `record_operation(message, *, level="info", source="应用", source_path="", session=None, title=None, detail="")` | 向窗口的操作日志追加记录，返回 `None`。级别支持 `info`、`warning` 和 `error`，其他值会抛出 `ValueError`。 |

成功打开文件、首次保存到路径或另存为后，最近文件菜单会反映最新路径。日志的 `session` 可传视图或文件路径，运行时据此填充文件名和来源路径，未命名视图显示为“未命名”。提供 `title` 时，面板用 `title` 和 `detail` 展示摘要，复制使用归一为单行的 `message`，最多保留 2048 个字符。面板只在内存中保留当前窗口最近 1000 条记录，支持选择复制、复制全部和清空。

## 插件接入 API

插件从 `workbench_api` 导入核心协议，从 `workbench_api.qt` 导入界面声明。运行时在激活时传入按插件分配的 `PluginContext`，注册、文档查询和 UI 操作通过上下文服务完成。详见[核心协议](../workbench-api/src/workbench_api/contracts.py)和 [Qt 扩展协议](../workbench-api/src/workbench_api/qt.py)。

### 清单与生命周期

文本插件在自己的 `pyproject.toml` 中声明入口，入口名称 `text` 对应 `PluginManifest.id`。

```toml
[project.entry-points."personal_workbench.plugins"]
text = "workbench_plugin_text:manifest"
```

入口指向包内的 `manifest` 对象。业务实现延迟到激活函数中导入。

```python
from workbench_api import PluginManifest


def activate(context):
    """使用宿主分配的上下文创建文本插件并注册其编辑器。"""
    # 发现阶段只读取轻量清单，启用后才加载编辑器实现。
    from .editor import TextPlugin

    return TextPlugin(context)


manifest = PluginManifest("text", "文本编辑", "0.1.0", activate)
```

`PluginManifest(id, name, version, activate, api_version=API_VERSION, requires=())` 声明稳定的插件 ID、显示名、插件版本和激活函数。`api_version` 须与宿主的 `API_VERSION` 一致，当前值为 `2`。`requires` 填写必须先启用的插件 ID。第 2 版通过下述界面与文档服务提供面板、主题和标签操作。详见[文本插件入口声明](../../plugins/workbench-plugin-text/pyproject.toml)和[清单实现](../../plugins/workbench-plugin-text/src/workbench_plugin_text/__init__.py)。

1. 启动时读取清单和保存的启用偏好，默认启用候选插件，依赖先于使用它的插件激活。入口加载异常、重复 ID、版本冲突、依赖循环或依赖不可用会标记相关插件失败，其余可用插件继续启动。
2. `activate(context)` 注册能力并返回提供 `dispose()` 的插件实例。激活异常时，运行时通过上下文撤销已完成的注册与订阅。
3. 正常退出先释放文档视图，再按激活的逆序调用插件实例的 `dispose()`，随后逆序清理该插件上下文拥有的资源。

### 编辑器与文档

`context.editors.register(provider) -> Disposable` 注册 `EditorProvider(id, title, extensions, new_document, open_document, icon="document")`。提供者 ID 与扩展名在窗口内须唯一，扩展名以小写和前导点声明，如 `(".txt", ".md")`。插件 ID 标识整个插件，提供者 ID 标识一种编辑器，例如 `text` 与 `text.editor`。

- `new_document(path)` 接收 `Path` 或 `None`。普通打开调用 `open_document(path, None, None)`。恢复时调用 `open_document(path, view_id, state)`，使用记录中的视图 ID 和视图状态。
- 提供者在业务对象和控件初始化完成后调用 `context.documents.register_view()`，注册失败时负责清理候选对象。成功返回 `OperationResult(view_id=...)`，用户取消返回 `OperationResult.cancelled(message="")`，失败返回 `OperationResult.failed(message, reported=False)`。失败参数也可传原始异常。已写入错误日志时传 `reported=True`，让宿主避免重复记录。结果对象的布尔值仅在成功时为 `True`，可用 `status` 区分取消和失败。
- `DocumentHandle` 表示共享文档，提供 ID、路径、资源 URI、修改状态、保存策略和 `save(*, save_as=False, reason="explicit")`。宿主按操作传入 `explicit`、`batch` 或 `close`，插件负责提交关联视图的输入、执行写入并返回结果。
- `EditorView` 表示独立标签，提供视图 ID、标题、共享文档、`QWidget` 和标准编辑动作。通过 `prepare_input()` 报告输入是否可提交，通过 `capture_state()` 和 `restore_state(state)` 保存或应用视图状态。复制标签由 `duplicate()` 创建共享原文档的新视图，`dispose()` 负责释放当前视图资源。视图可另外提供 `menu_actions`、`toolbar_actions` 和 `status_text`。

| 文档服务 | 返回值与行为 |
| --- | --- |
| `context.documents.active` | 当前活动视图，没有活动视图时返回 `None`。恢复失败的标签可能是占位视图，访问编辑器特有属性前应确认视图类型。 |
| `context.documents.ordered()` | 按当前分屏和标签顺序返回视图列表。 |
| `context.documents.find_path(path)` | 按规范化路径查询，优先返回匹配的活动视图，否则按布局顺序返回首个匹配视图，找不到时返回 `None`。查询可用于检测跨插件路径冲突。 |
| `context.documents.activate_view(view_id)` | 选中已打开的标签及其所在分屏，并聚焦视图控件。活动视图变化时发出原有通知，标识未知时抛出 `ValueError`。 |
| `context.documents.register_view(provider_id, view, *, after=None, remember=True)` | 使用当前插件已注册的提供者登记并激活视图，返回 `Disposable`。可用 `after` 将视图插入指定标签之后。恢复或复制标签时传 `remember=False` 可保留最近文件顺序。 |
| `context.documents.sync(view)` | 同步标签标题、修改标记及窗口状态，返回 `None`。 |
| `context.documents.remove_view(view_id)` | 直接释放当前插件拥有的视图，返回 `None`。用于注册回滚或插件清理。正常关闭标签由宿主执行保存检查。 |

比较磁盘路径时，先转为绝对路径并解析符号链接，再用 `os.path.normcase()` 规范化路径，Windows 下会统一大小写。注册时，每个标签须有唯一 ID，同一路径的多个标签须引用同一个共享文档对象，不同文档也须使用不同文档 ID。普通打开复用已有标签，复制标签创建独立视图。例如文本插件的两个标签共享内容和撤销历史，各自保存光标、选区和滚动位置。

### 命令、面板与事件

| 上下文服务 | 返回值与行为 |
| --- | --- |
| `commands.register(spec)` | 接收 `CommandSpec`，返回可复用的 `QAction`。声明包含 ID、标题和回调，可指定快捷键、图标、菜单和 `enabled()`。标识或快捷键冲突时抛出 `ValueError`。文档状态刷新时重新计算命令可用性。 |
| `ui.register_panel(spec)` | 接收 `PanelSpec`，由宿主按需调用控件工厂。可声明停靠位置、图标、快捷键、标题动作和默认显隐。设置了快捷键时返回工具动作，否则返回 `None`。 |
| `ui.panel_visible(panel_id)` | 读取布局中的显隐偏好，暂停面板时保留该值。 |
| `ui.activate_panel(panel_id, *, focus=True)` | 激活可用面板并返回 `True`，面板暂停或不可用时返回 `False`。传入 `focus=False` 可保留输入焦点。 |
| `ui.set_panels_suspended(panel_ids, suspended)` | 批量暂停或恢复面板，保留位置、尺寸和显隐偏好。先校验全部标识，再更新状态。以上三种面板操作遇到未知标识均抛出 `KeyError`。 |
| `ui.theme_color(token)`、`ui.theme_icon(name)` 和 `ui.compact` | 分别提供当前主题的 Qt 颜色、图标和紧凑模式。 |
| `ui.contribute_menu(contribution)` | 接收 `MenuContribution`。构建指定菜单时调用 `populate(menu)`，可根据当前文档加入动作。返回 `Disposable`。 |
| `ui.register_status(spec)` | 接收 `StatusItemSpec`，向状态栏添加控件，随文档状态变化计算 `visible()`。返回 `Disposable`。 |
| `ui.subscribe_active(callback)` | 订阅活动视图变化，回调收到当前视图或 `None`。返回 `Disposable`。 |
| `ui.subscribe_theme(callback)` 和 `ui.subscribe_layout(callback)` | 分别接收主题名称和无参数的布局变更通知。各自返回 `Disposable`。 |
| `ui.parent` 和 `ui.restoring` | 分别提供对话框父控件和启动恢复标志。插件可据此延后恢复期间的历史记录等副作用。 |
| `ui.action(identifier)` 和 `ui.current_layout()` | 分别返回已注册的 `QAction` 和当前布局字典。 |
| `ui.show_message(message)` 和 `ui.refresh()` | 分别显示状态提示和刷新文档相关界面，均返回 `None`。 |
| `ui.open_directory(view_id=None)` 和 `ui.remember_path(path)` | 分别打开指定视图的所在目录和更新最近文件，返回值沿用对应的宿主方法。 |

命令和菜单贡献使用宿主已有菜单名称，如“文件”“编辑”“视图”或“设置”。面板的稳定 ID 还用于恢复布局。宿主的布局行为详见[停靠与分屏](docs/停靠与分屏.md)。

### 设置、状态、日志与资源

| 上下文服务 | 返回值与行为 |
| --- | --- |
| `plugin_id` 和 `options` | 分别提供当前插件 ID 和宿主传入的启动选项。需要持久化的数据可写入 `settings` 或 `state`。 |
| `settings.value(key, default=None)`、`settings.setValue(key, value)`、`settings.sync()` | 按插件 ID 隔离偏好。读取返回值的副本，设置更新内存，调用 `sync()` 写入设置文件。 |
| `state.get(key, default=None)`、`state.set(key, value)`、`state.flush()` | 按插件 ID 隔离会话状态。读取返回值的副本，设置更新内存，调用 `flush()` 请求写入状态文件，受恢复和只读保护约束。 |
| `log(message, *, level="info", source="应用", source_path="", title=None, detail="")` | 追加结构化操作日志，返回 `None`。插件可填写文件名、完整路径和展示摘要。 |
| `own(resource)` | 接收 `Disposable` 或无参清理回调，返回幂等的 `Disposable`。可用来登记插件自建的信号连接或计时器，以便回滚和退出时统一清理。 |

设置和状态应使用可序列化为 JSON 的值，更新和写入方法均返回 `None`。宿主服务自动追踪已注册的资源，返回的注销句柄可用于提前释放。命令或面板返回的 `QAction` 用于界面复用，资源清理由上下文管理。

## 保存、关闭与会话恢复

### 两阶段关闭

关闭单个标签、批量关闭标签和退出窗口都先检查输入，再按共享文档处理保存。

1. 运行时依次调用目标视图的 `prepare_input()`，遇到失败便中止关闭。例如文本编辑器可在输入法候选期间拒绝本次关闭。
2. 每份目标文档只决策一次。`SavePolicy.AUTOMATIC` 在关闭时尝试保存。`SavePolicy.MANUAL` 在文档有修改且最后一个视图也要关闭时询问保存、放弃或取消。仍有其他视图保留的手动保存文档继续由剩余标签持有。
3. 全部通过后才关闭整批标签。取消、输入检查失败或保存失败时，待关闭标签全部保留，之前已经成功写入的文件继续生效。退出窗口还会保存布局、文档快照和设置，再释放资源。写入文档快照或设置出现 `OSError` 时，运行时会提示并保持窗口打开。

### 配置文件

配置位于 `storage_dir`，文件按版本使用 UTF-8 JSON，并通过同目录临时文件原子替换。

| 文件 | 内容 |
| --- | --- |
| `workbench-state-v1.json` | 退出时的文档视图、活动视图 ID、最近文件和插件状态，也保存布局的回退副本。每条文档记录保存路径、插件 ID、提供者 ID、视图 ID、共享文档 ID 和视图状态。 |
| `workbench-layout-v1.json` | 停靠会话，包括窗口位置和尺寸、分屏、标签顺序、工具面板和主题。优先从此文件加载，缺少时尝试状态文件中的布局。 |
| `workbench-settings-v1.json` | 宿主与插件设置，包括窗口置顶偏好和下次启动使用的插件启用状态。 |

恢复以已有路径和视图状态为基础，正文由插件从实际文件读取。未保存且尚无路径的标签只存在于当前运行。启动时先激活插件，再打开记录中的文档，最后恢复布局和活动标签。布局的自动保存推迟到恢复完成后，防止初始布局覆盖上次会话。

文件缺失、打开失败或所属插件不可用时，运行时会展示可关闭的占位标签并保留原记录。原活动视图不可用时，优先选中可用视图。之后成功打开对应路径时，真实视图会替换占位记录。

`restore=False` 仅跳过重开文档和加载布局，正常退出仍可保存本次会话。状态或设置文件损坏时会尝试备份原始字节并继续启动，备份失败时保护对应原文件。布局无效时回退到初始布局，文档会话单独恢复。

### 产品迁移

`migration(directory)` 接收最终配置目录的 `Path`。返回的 `initial_state` 可为 `None`，或包含 `schema_version=1` 的状态字典。若提供视图记录，每条须包含 `id`、`plugin_id`、`editor_type` 和绝对 `path`，可携带 `document_id` 与 `view_state`。返回的 `initial_settings` 是按命名空间分组的字典，空字符串命名空间用于宿主设置，插件 ID 用于对应插件。

已成功读取的新版状态和设置各自优先，迁移返回值作为加载回退。旧路径、旧字段的解释和只读导入由产品实现。个人工作台的历史目录和迁移逻辑可见[产品装配](../../apps/personal-workbench/src/personal_workbench/app.py)和[迁移实现](../../apps/personal-workbench/src/personal_workbench/migration.py)。

## 开发验证

在仓库根目录同步工作区后，运行本包测试或构建 wheel。

```powershell
uv run --locked --no-sync pytest -q packages/workbench-runtime
uv build --package workbench-runtime
```

测试覆盖宿主配置、插件启用和激活回滚、文档去重和关闭、状态文件校验以及操作日志。文档用例通过通用宿主加载真实文本插件。详见[模块测试](tests/)。
