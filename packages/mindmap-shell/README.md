# mindmap-shell

思维导图项目内置的原生桌面外壳，Python 导入名为 `mindmap_shell`。
支持 Python 3.12、3.13，依赖 `mindmap-docking`，不导入导图业务、画布或引擎。

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

`AppSpec` 声明稳定应用 ID、标题、版本、图标、主题和配置目录。
`ActionSpec` 描述由菜单、标题栏按钮及快捷键共享的业务动作；`WorkbenchWindow` 通过 `manager` 暴露停靠管理器。

集成标题栏左侧仅保留应用图标与主菜单入口，不显示应用名称、副标题或斜杠分隔符。
应用名称通过图标悬停提示、辅助技术名称和系统窗口标题提供，文档名称由中央标签显示。

集成标题栏采用略深的背景、一像素细边框和 Windows 系统阴影。细边框是透传输入的覆盖层，
不增加内容边距，不改变停靠区域、按钮排列或状态栏高度；最大化、全屏时收起轮廓和外阴影。
设置 `native_title_bar=True` 时继续使用系统自身的窗口装饰。其他平台或系统合成不可用时保留
细边框与标题栏配色；离屏截图只包含 Qt 绘制的内容，不包含系统在窗口外部绘制的投影。
业务可以通过 `ThemeTokens` 的 `main-window-titlebar-bg`、`main-window-outline` 覆盖这两项颜色，
并分别提供浅色和深色值。系统阴影接入方式参见 [Windows 自定义窗口框架文档](https://learn.microsoft.com/windows/win32/dwm/customframe)。
`run_desktop(builder, argv, ...)` 处理主题、紧凑布局、窗口尺寸、独立配置目录和截图退出等通用启动参数，
调用方通过 `configure_parser` 添加业务参数，通过窗口的关闭决策管理数据保存。

外壳拥有窗口控件，业务拥有数据和动作回调；辅助窗口复用同一规格与动作，不重复注册业务操作。
所有界面操作在 Qt GUI 线程调用。公共参数与生命周期见[外壳配置](src/mindmap_shell/specs.py)、
[主窗口](src/mindmap_shell/window.py)、[启动器](src/mindmap_shell/cli.py)。
实际导图应用的接入方式见[桌面适配层](../../apps/mindmap-workbench/src/mindmap_workbench/shell_adapter.py)。

## 可选项目模板

随包保留一个独立桌面项目模板，生成器入口改名为 `mindmap-new`。

```powershell
uv run --locked --all-packages mindmap-new ../example-desktop --name example-desktop --title "示例桌面应用" --framework-root .
```

`--framework-root` 指向当前 mindmap 仓库，将生成工程的依赖绑定到本仓库两个基础包。
从 wheel 安装后也可使用生成器；未指定源码位置时保留普通版本依赖，需要提供已构建的 wheel 或发布包。
目标目录必须尚不存在，生成器不会覆盖已有目录。模板唯一来源为本包的 `project_template/`。

资源与 MIT 许可见[资源与许可](../../docs/资源与许可.md)和 [LICENSE](LICENSE)。
