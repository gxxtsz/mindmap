# 个人工作台

基于 Python、PySide6 和 uv workspace 的本地个人工作台。平台提供文档标签、分屏、命令、面板、设置、日志和会话恢复；功能通过插件接入。

## 快速开始

桌面应用支持 Python 3.12 和 3.13。项目使用 uv 管理环境和依赖，请在仓库根目录运行以下命令。

```powershell
uv sync --locked --all-packages --group dev
uv run --locked --package personal-workbench personal-workbench
```

也可运行 `uv run --locked --package personal-workbench python -m personal_workbench`。
命令末尾可追加一个待打开文件。下表列出常用参数。

| 参数 | 用途 |
| --- | --- |
| `--theme light\|dark` | 指定明暗主题 |
| `--state-dir 目录` | 使用独立配置目录 |
| `--no-restore` | 以默认布局启动，跳过上次会话的标签和布局恢复，仍读取设置和最近文件 |
| `--compact` | 使用紧凑外壳 |
| `--size 宽x高` | 指定窗口逻辑尺寸，默认 `1440x900` |
| `--screenshot 文件.png` | 保存窗口截图及同名 JSON 元数据后退出 |

## 内置插件

| 插件 | 功能与保存方式 |
| --- | --- |
| [思维导图](plugins/workbench-plugin-mindmap/README.md) | `.mindmap` 编辑、布局、图片、页面和业务面板。绑定保存路径后防抖自动保存。 |
| [软件结构可视化](plugins/workbench-plugin-software-structure/README.md) | 打开 `.software-structure` 文档并只读浏览模块依赖图。 |

内置插件默认启用，可在“设置 → 插件管理”中调整，重启生效。相同导图的多个视图共享内容与撤销历史，各自保留页面和导航状态。
关闭时统一检查输入和保存决策，取消或失败时保留待关闭标签。会话配置只保存文档路径、视图状态和布局等元数据。

## 目录结构

```text
apps/personal-workbench/          产品入口、配置迁移和工作区分发验收
packages/
  workbench-api/                 插件与编辑器公开协议
  workbench-runtime/             插件宿主、文档与会话、桌面外壳与停靠
plugins/
  workbench-plugin-mindmap/
    core/                       模型、编辑引擎、内置布局、文档格式
    qt-render/                  Qt 测量、静态绘制与图形导出
    qt-editor/                  Qt 交互画布、业务面板和共享控件
    workbench-adapter/          工作台插件入口、文档会话与自动保存
  workbench-plugin-software-structure/  软件结构文档、模块依赖图和 AI 生成模板
```

工作区包含 **8 个独立分发包**。通用包使用 `workbench-*` 名称，导图核心、渲染和编辑器使用 `mindmap-*`，内置插件适配包使用 `workbench-plugin-*`，产品入口为 `personal-workbench`。
各分发包的源码和测试分别位于自身的 `src/` 和 `tests/` 目录。运行时通过入口点发现插件。

## 配置兼容

工作台默认在系统配置目录下的 `mindmap/mindmap-workbench` 保存会话和设置。配置文件与迁移条件见[应用配置兼容](apps/personal-workbench/README.md#配置兼容)。

`.mindmap` 的读写入口和格式版本规则见[导图核心的文档读写说明](plugins/workbench-plugin-mindmap/core/README.md#文档读写)。

## 检查与构建

同步环境后，在仓库根目录运行以下命令。pytest 从 `packages/`、`plugins/` 和 `apps/` 收集各包的测试。

```powershell
uv run --locked --no-sync pytest
uv run --locked --no-sync ruff check .
uv lock --check
uv build --all-packages
```

## 开发文档

- 插件协议、生命周期、文档服务和数据兼容见[工作台运行时](packages/workbench-runtime/README.md)。
- 导图组件的使用入口见[思维导图 Python 接口](plugins/workbench-plugin-mindmap/README.md#python-接口)。
