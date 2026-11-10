# 个人工作台

基于 Python、PySide6 和 uv workspace 的本地个人工作台。平台提供文档标签、分屏、命令、面板、设置、日志和会话恢复；功能通过插件接入。

## 快速开始

支持 Python 3.12、3.13；项目使用 uv 管理环境和依赖。在仓库根目录执行：

```powershell
uv sync --locked --all-packages --group dev
uv run --locked --package personal-workbench personal-workbench
```

也可运行 `uv run --locked --package personal-workbench python -m personal_workbench`。
命令末尾可追加一个待打开文件。常用参数：

| 参数 | 用途 |
| --- | --- |
| `--theme light\|dark` | 指定明暗主题 |
| `--state-dir 目录` | 使用独立配置目录 |
| `--no-restore` | 跳过标签与布局恢复 |
| `--compact` | 使用紧凑外壳 |
| `--size 宽x高` | 指定窗口尺寸 |
| `--screenshot 文件.png` | 截图并退出 |

## 内置插件

| 插件 | 功能与保存方式 |
| --- | --- |
| [思维导图](plugins/workbench-plugin-mindmap/README.md) | `.mindmap` 编辑、布局、图片、页面与业务面板；防抖自动保存 |

思维导图插件默认启用，可在“设置 → 插件管理”中调整，重启生效。相同导图的多个视图共享内容与撤销历史，各自保留页面和导航状态。
关闭时统一检查输入和保存决策，取消或失败时保留待关闭标签。会话配置只保存文档路径、视图状态和布局等元数据。

## 目录结构

```text
apps/personal-workbench/          产品入口、插件装配、旧配置迁移和工作区分发验收
packages/
  workbench-api/                 插件与编辑器公开协议
  workbench-runtime/             插件宿主、文档与会话、桌面外壳与停靠
plugins/
  workbench-plugin-mindmap/
    core/                       模型、编辑引擎、内置布局、文档格式
    qt-render/                  Qt 测量、静态绘制与图形导出
    qt-editor/                  Qt 交互画布、业务面板与共享控件
    workbench-adapter/          工作台插件入口、文档会话与自动保存
    tests/integration/          导图组件组合测试
    scripts/                    导图性能与桌面专项验收
```

工作区包含 **7 个独立分发包**。通用能力使用 `workbench-*` 名称，产品入口使用 `personal-workbench`，导图专属组件使用 `mindmap-*`。
分发包使用 `src/` 布局，单包测试随源码放置，插件专属组件集中到插件分组目录。新增插件不需要修改主窗口。

## 配置兼容

工作台默认将配置保存到系统配置目录下的 `mindmap/mindmap-workbench`。
`.mindmap` 文件格式和布局标识保持稳定。历史格式的配置只读导入，现有工作台配置在启动时升级后恢复。

## 检查与构建

```powershell
uv run --locked --no-sync pytest
uv run --locked --no-sync ruff check .
uv lock --check
uv build --all-packages
```

性能与导图专项命令见[导图验收脚本](plugins/workbench-plugin-mindmap/scripts/README.md)。

## 开发文档

- 插件协议、生命周期、文档服务和数据兼容见[工作台运行时](packages/workbench-runtime/README.md)。
- 导图组件的使用入口见[思维导图 Python 接口](plugins/workbench-plugin-mindmap/README.md#python-接口)。
