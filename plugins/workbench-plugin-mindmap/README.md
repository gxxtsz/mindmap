# 思维导图插件

通过公开工作台 API 提供 `.mindmap` 编辑器、业务面板、页面历史、共享视图和自动保存。
导图专属源码、组件测试和组合测试集中在本目录。

## 目录与分发边界

| 目录 | 内容 |
| --- | --- |
| [core](core/README.md) | `mindmap-core` 提供模型、事务、内置布局与文档格式，仅使用标准库 |
| [qt-render](qt-render/README.md) | `mindmap-qt-render` 提供 Qt 测量、静态绘制与图形导出 |
| [qt-editor](qt-editor/README.md) | `mindmap-qt-editor` 提供画布、业务面板、共享控件与文件控制器 |
| [workbench-adapter](workbench-adapter/README.md) | `workbench-plugin-mindmap` 负责插件注册、文档与视图会话、面板装配和自动保存 |

四个分发包直接注册到仓库根 uv 工作区，各自管理 `src/`、`tests/`、许可证和构建元数据。
从任意成员目录均可使用根锁文件及环境。

画布、面板与引擎的组合回归位于 [Qt 编辑器集成测试](qt-editor/tests/integration/)，工作台中的文档会话和自动保存回归位于[适配包测试](workbench-adapter/tests/)。

## 运行

插件随个人工作台启动，环境同步和运行命令见[应用启动说明](../../apps/personal-workbench/README.md#启动)。

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
| 工作台插件清单 | `from workbench_plugin_mindmap import manifest` |

## 工作台接入

- 导图的入口点声明和生命周期见[工作台适配包](workbench-adapter/README.md)。
- 宿主的插件发现、依赖校验和激活机制见[工作台运行时](../../packages/workbench-runtime/README.md#插件接入-api)。
