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
| [文本编辑](plugins/workbench-plugin-text/README.md) | `.txt`、`.md` 纯文本编辑；手动保存、UTF-8/BOM 和原子写入 |

两个插件默认启用，可在“设置 → 插件管理”中调整，重启生效。相同文件的多个视图共享内容与撤销历史，各自保留导航或光标状态。
关闭时统一检查输入和保存决策；取消或失败保留待关闭标签。未保存文本正文不写入会话配置。

## 目录结构

```text
apps/personal-workbench/          产品入口、内置插件装配和旧配置迁移
packages/
  desktop/
    workbench-docking/           通用停靠、标签、分屏、主题和布局
    workbench-shell/             通用窗口、启动器和项目脚手架
  platform/
    workbench-api/               插件与编辑器公开协议
    workbench-runtime/           插件、文档、命令和会话协调
plugins/
  workbench-plugin-text/         文本插件
  workbench-plugin-mindmap/
    adapter/                    导图插件接入层
    engine/                     核心、布局、格式三个独立包
    qt/                         渲染、控件、画布、面板四个独立包
    tests/integration/          导图组件组合测试
    scripts/                    导图性能与桌面专项验收
    docs/                       导图操作说明
scripts/                        工作区发现、发行和混合编辑器验收
tests/integration/              跨包架构、桌面组合和进程生命周期验收
docs/                           通用架构、开发与发行说明
```

工作区仍包含 **14 个独立分发包**。通用能力使用 `workbench-*` 名称；产品入口使用 `personal-workbench`；导图专属组件保留 `mindmap-*`。
分发包使用 `src/` 布局，单包测试随源码放置，插件专属组件集中到插件分组目录。新增插件不需要修改主窗口。

## 配置兼容

默认配置位置继续使用系统配置目录下的 `mindmap/mindmap-workbench`。产品入口注入历史应用标识及路径，通用运行时不解释历史产品名称。
`.mindmap` 文件格式和布局标识保持稳定；旧配置只读导入，已有新版配置直接恢复。

## 检查与构建

```powershell
uv run --locked --no-sync pytest
uv run --locked --no-sync ruff check .
uv build --all-packages
uv run --locked --no-sync python scripts/verify_wheels.py
uv run --locked --no-sync python scripts/verify_plugins.py
```

生成独立桌面工程可使用 `uv run --locked --package workbench-shell workbench-new 目标目录`。
性能与导图专项命令见[导图验收脚本](plugins/workbench-plugin-mindmap/scripts/README.md)。

## 开发文档

- [插件化工作台](docs/插件化工作台.md)：插件协议、生命周期、文档服务和数据兼容。
- [桌面框架](docs/桌面框架.md)：停靠、外壳与平台的职责边界。
- [开发与验收](docs/开发与验收.md)：完整成员说明、构建、桌面和性能验证。
- [资源与许可](docs/资源与许可.md)：字体、图标和其他资源来源。
