# 思维导图插件

通过公开工作台 API 提供 `.mindmap` 编辑器、业务面板、页面历史、共享视图和自动保存。
导图专属源码、组件测试、组合测试和验收脚本集中在本目录。

## 目录与分发边界

| 目录 | 内容 |
| --- | --- |
| [core](core/README.md) | `mindmap-core` 提供模型、事务、内置布局与文档格式，仅使用标准库 |
| [qt-render](qt-render/README.md) | `mindmap-qt-render` 提供 Qt 测量、静态绘制与图形导出 |
| [qt-editor](qt-editor/README.md) | `mindmap-qt-editor` 提供画布、业务面板、共享控件与文件控制器 |
| [workbench-adapter](workbench-adapter/README.md) | `workbench-plugin-mindmap` 负责插件注册、文档与视图会话、面板装配和自动保存 |
| `tests/integration/` | 引擎、格式、画布与面板的业务组合验收 |
| [scripts](scripts/README.md) | 导图性能基准、绘制和桌面专项验收 |

四个分发包直接注册到仓库根 uv 工作区，各自管理 `src/`、`tests/`、许可证和构建元数据。
从任意成员目录均可使用根锁文件及环境。

## 运行

在仓库根目录执行：

```powershell
uv sync --locked --all-packages --group dev
uv run --locked --package personal-workbench personal-workbench
```

## Python 接口

| 用途 | 导入路径 |
| --- | --- |
| 文档模型、编辑引擎与扩展协议 | `mindmap_core` |
| 内置布局 | `mindmap_core.layouts` |
| 编解码与文件读写 | `mindmap_core.formats` |
| 静态渲染与图形导出 | `mindmap_qt_render` |
| 交互画布与文件控制器 | `mindmap_qt_editor.canvas` |
| 业务面板与宿主协议 | `mindmap_qt_editor.panels` |
| 共享控件、色板与图标 | `mindmap_qt_editor.controls`、`mindmap_qt_editor.theme`、`mindmap_qt_editor.icons` |
| 工作台插件清单 | `workbench_plugin_mindmap:manifest` |

## 开发与验收

- 公开协议、注册、生命周期及兼容策略见[工作台运行时](../../packages/workbench-runtime/README.md)。
- 导图性能与桌面专项验收见[导图专项脚本](scripts/README.md)。
