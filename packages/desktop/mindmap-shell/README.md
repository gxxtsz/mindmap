# mindmap-shell

思维导图项目内置的原生桌面外壳，Python 导入名为 `mindmap_shell`。

- **运行环境与依赖：** 支持 Python 3.12、3.13，依赖 `mindmap-docking`，不导入导图业务、画布或引擎。

## 目录

- [安装与构建](#安装与构建)
- [应用接入](#应用接入)
- [标题栏与导航](#标题栏与导航)
- [窗口控制与外观](#窗口控制与外观)
- [启动与生命周期](#启动与生命周期)
- [可选项目模板](#可选项目模板)
- [相关接口与文档](#相关接口与文档)

## 安装与构建

本包属于根目录 uv workspace，源码、依赖和资源全部位于当前仓库。

```powershell
uv sync --locked --all-packages --group dev
uv build --package mindmap-shell
```

## 应用接入

```python
from mindmap_docking import DocumentSpec
from mindmap_shell import AppSpec, WorkbenchWindow, run_desktop
from PySide6.QtWidgets import QLabel

def build_window(options):
    window = WorkbenchWindow(AppSpec("example-desktop", "示例桌面应用", storage_dir=options.state_dir))
    window.manager.register_document(DocumentSpec("welcome", "欢迎", lambda: QLabel("业务内容")))
    return window

raise SystemExit(run_desktop(build_window))
```

- **应用配置：** `AppSpec` 声明稳定应用 ID、标题、版本、图标、主题和配置目录。

- **业务动作与管理器：** `ActionSpec` 描述由菜单、标题栏按钮及快捷键共享的业务动作；`WorkbenchWindow` 通过 `manager` 暴露停靠管理器。

## 标题栏与导航

### 菜单与名称

- 集成标题栏左侧默认显示应用图标与主菜单入口，不显示应用名称、副标题或斜杠分隔符。

- 应用名称通过图标悬停提示、辅助技术名称和系统窗口标题提供，文档名称由中央标签显示。

- 外观与布局设置统一放在左侧主菜单中，右侧仅放业务动作及窗口控制按钮，不再显示设置齿轮。

### 业务导航插槽

- `WorkbenchWindow.set_title_bar_navigation(widget)` 可在主菜单右侧、伸缩留白之前安装一个业务导航控件。

- 接口接受 `QWidget` 或 `None`，重复设置同一实例不重复插入；传入 `None` 清空插槽。

- 外壳接管控件的父子所有权，替换或清空时隐藏并延迟销毁旧控件；控件的尺寸、主题细节和业务行为由调用方负责。

- 导航插槽不改变右侧工具动作或窗口控制按钮的顺序；思维导图工作台利用该插槽显示当前导图和最近文件列表。

### 紧凑布局

- 窗口采用紧凑双层布局：普通模式标题栏为38逻辑像素，紧凑模式为36逻辑像素，工作区顶部留白为2逻辑像素；主菜单保留32×32逻辑像素的点击区域。

- 窗口控制按钮采用轻量圆角方案：16逻辑像素图标、40×32逻辑像素点击区域、4逻辑像素圆角，组内间距为2逻辑像素。

- **文档标签：** 文档标签仍独立归属各自分屏，高度沿用32／28逻辑像素；普通模式较原布局收回18逻辑像素的顶部空间。

## 窗口控制与外观

### 置顶接口

- 右上角依次为置顶、最小化、最大化／还原和关闭；置顶使用图钉按钮，选中时以主题强调色高亮，悬停提示在“置顶窗口”和“取消置顶”之间切换，普通与紧凑模式均保持同一点击尺寸。

- `WorkbenchWindow.is_always_on_top()` 读取实际窗口置顶标志；`set_always_on_top(enabled)` 设置置顶，保留可见性、位置、尺寸及最大化／最小化状态，首次显示前调用不会提前显示窗口。

- 状态改变后发送 `always_on_top_changed(bool)`，重复设置相同状态不重复通知；宿主负责选择持久化方式。

- 接口在 Qt GUI 线程调用；系统标题栏模式下自绘图钉随其他窗口按钮隐藏，公开接口仍可使用。

### 按钮反馈与系统贴靠

- 最小化与最大化按钮使用独立的悬浮和按下底色，浅色分别为 `#D5DEE9`、`#BFCBDD`，深色分别为 `#393B40`、`#4A4D55`；关闭按钮悬浮和按下时使用红底白图标。

- 业务可通过 `ThemeTokens` 的 `window-control-bg-hovered`、`window-control-bg-pressed` 分别覆盖两种主题的普通窗口按钮反馈，不影响工具栏的通用悬浮配色。

- Windows 最大化按钮同时同步非客户区悬浮、按压和抓取状态，保留系统贴靠菜单；按住移出、外部松开、失焦或抓取被其他控件接管时取消操作，移出后返回按钮再松开可正常最大化或还原。

### 边框、阴影与主题

- 集成标题栏采用略深的背景、一像素细边框和 Windows 系统阴影。

- 细边框是透传输入的覆盖层，不增加内容边距，不改变停靠区域、按钮排列或状态栏高度；最大化、全屏时收起轮廓和外阴影。

- 设置 `native_title_bar=True` 时继续使用系统自身的窗口装饰。

- 其他平台或系统合成不可用时保留细边框与标题栏配色；离屏截图只包含 Qt 绘制的内容，不包含系统在窗口外部绘制的投影。

- 业务可以通过 `ThemeTokens` 的 `main-window-titlebar-bg`、`main-window-outline` 覆盖这两项颜色，并分别提供浅色和深色值。

- 系统阴影接入方式参见 [Windows 自定义窗口框架文档](https://learn.microsoft.com/windows/win32/dwm/customframe)。

## 启动与生命周期

### 启动参数

- `run_desktop(builder, argv, ...)` 处理主题、紧凑布局、窗口尺寸、独立配置目录和截图退出等通用启动参数，调用方通过 `configure_parser` 添加业务参数，通过窗口的关闭决策管理数据保存。

### 离屏剪贴板清理

- 离屏平台的剪贴板由进程自行维护。

- 启动器在事件循环结束后、`QApplication` 销毁前清理离屏 MIME 载荷，避免 Python 创建的 `QMimeData` 在解释器退出阶段触发原生访问冲突；原生平台继续保留系统剪贴板内容。

- 自行创建离屏应用的验收脚本也须在 Qt 仍有效时完成相同清理，不能用强制退出跳过析构。

### 宿主约束

- 外壳仅承载一个主窗口，业务拥有数据和动作回调；同一管理器不再支持辅助窗口。

- 共用菜单不再内置工作区菜单、工具搜索和重复关闭入口；工作台在业务文件菜单中提供退出操作。

- `WorkbenchWindow` 的宿主标识仅允许 `main`，窗口控制按钮仍由系统窗口状态管理。

## 可选项目模板

随包保留一个独立桌面项目模板，生成器入口改名为 `mindmap-new`。

```powershell
uv run --locked --all-packages mindmap-new ../example-desktop --name example-desktop --title "示例桌面应用" --framework-root .
```

- `--framework-root` 指向当前 mindmap 仓库，将生成工程的依赖绑定到本仓库两个基础包。

- 从 wheel 安装后也可使用生成器；未指定源码位置时保留普通版本依赖，需要提供已构建的 wheel 或发布包。

- 目标目录必须尚不存在，生成器不会覆盖已有目录。

- 模板唯一来源为本包的 `project_template/`。

## 相关接口与文档

- 所有界面操作在 Qt GUI 线程调用。

- 公共参数与生命周期见[外壳配置](src/mindmap_shell/specs.py)、[主窗口](src/mindmap_shell/window.py)、[启动器](src/mindmap_shell/cli.py)。

- 实际导图应用的接入方式见[桌面适配层](../../../apps/mindmap-workbench/src/mindmap_workbench/shell_adapter.py)。

- 资源与 MIT 许可见[资源与许可](../../../docs/资源与许可.md)和 [LICENSE](LICENSE)。
