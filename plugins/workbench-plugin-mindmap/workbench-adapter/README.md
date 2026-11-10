# 思维导图工作台适配包

`workbench-plugin-mindmap` 通过公开 API 向个人工作台注册导图编辑器、菜单、面板和状态栏，
管理共享文档、独立视图、页面历史、自动保存及后台任务。

该包与核心、渲染、Qt 编辑器三个组件共同归属上级插件目录，各自独立构建和安装。
所有成员向上发现仓库根工作区，共用 `.venv` 与 `uv.lock`。
分发成员和组件导入路径见[思维导图插件说明](../README.md)。公开插件机制见[工作台运行时](../../../packages/workbench-runtime/README.md)。

## 启动与注册

插件随工作台启动。同步环境和启动应用的命令见[应用启动说明](../../../apps/personal-workbench/README.md#启动)。

发行名为 `workbench-plugin-mindmap`，Python 导入名为 `workbench_plugin_mindmap`。
`personal_workbench.plugins` 入口点组以 `mindmap` 注册 `workbench_plugin_mindmap:manifest`。
发现阶段读取轻量清单，启用后的激活阶段才导入导图业务实现。

打开导图后，「文件 → 导出当前页」提供 PNG、SVG、PDF 和 TXT 四种格式。「编辑」保留常用的添加节点、编辑文字和节点属性入口，其余动作按节点结构、页面、灵感和图片归组。导图设置及其他可用侧栏统一列在「视图 → 面板」。

## 目录与依赖

- `src/workbench_plugin_mindmap/`：插件装配、文档与视图会话、业务日志及页面历史。
- `tests/`：导图自动保存、共享视图、收藏、灵感及页面恢复等回归测试。
- 运行时依赖 `mindmap-core`、`mindmap-qt-render`、`mindmap-qt-editor`、`workbench-api` 和 PySide6。工作台接入使用 `workbench-api` 的公开协议。
- 源码发行包包含本包的源码、测试和构建元数据。wheel 包含本包的运行代码，由安装器按分发元数据解析依赖。

## 文档与视图生命周期

- `documents.py` 的 `MindMapDocumentHandle` 持有共享引擎、文件状态、撤销历史、自动保存计时器和后台任务。最后一个视图释放后才结束文档生命周期。
- `views.py` 的 `MindMapEditorView` 持有每个标签独立的画布、面板、导航和输入状态，通过共享文档取得内容。
- `editor_provider.py` 的 `MindMapEditorProvider` 创建、打开和复制视图，并通过公开文档服务登记。平台负责通用标签和批量关闭，提供者只协调导图专属装配。
- `controller.py` 注册轻量清单激活后的菜单、面板、状态栏和偏好，不承担文档存储。

画布与面板的组合用例见 [Qt 编辑器集成测试](../qt-editor/tests/integration/)。
