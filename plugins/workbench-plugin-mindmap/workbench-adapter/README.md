# 思维导图工作台适配包

`workbench-plugin-mindmap` 通过公开 API 向个人工作台注册导图编辑器、菜单、面板和状态栏，
管理共享文档、独立视图、页面历史、自动保存及后台任务。

该包与核心、渲染、Qt 编辑器三个组件共同归属上级插件目录，各自独立构建和安装。
所有成员向上发现仓库根工作区，共用 `.venv` 与 `uv.lock`。
完整导图操作及组件接口索引见[思维导图插件说明](../README.md)，公开插件机制见[工作台运行时](../../../packages/workbench-runtime/README.md)。

## 安装与启动

在仓库根目录执行：

```powershell
uv sync --locked --all-packages --group dev
uv run --locked --package personal-workbench personal-workbench
```

发行名保持 `workbench-plugin-mindmap`，Python 导入名保持 `workbench_plugin_mindmap`，
入口为 `workbench_plugin_mindmap:manifest`。禁用插件时只读取轻量清单，不加载业务实现。

## 目录与依赖

- `src/workbench_plugin_mindmap/`：插件装配、文档与视图会话、业务日志及页面历史。
- `tests/`：导图自动保存、共享视图、收藏、灵感及页面恢复等回归测试。
- 核心、渲染和 Qt 编辑器通过分发包依赖安装，工作台接入通过 `workbench-api` 的公开协议完成。
- 源码发行包和 wheel 收录本包内容，运行时依赖由分发元数据声明。

## 文档与视图生命周期

- `documents.py` 的 `MindMapDocumentHandle` 持有共享引擎、文件状态、撤销历史、自动保存计时器和后台任务。最后一个视图释放后才结束文档生命周期。
- `views.py` 的 `MindMapEditorView` 持有每个标签独立的画布、面板、导航和输入状态，通过共享文档取得内容。
- `editor_provider.py` 的 `MindMapEditorProvider` 创建、打开和复制视图，并通过公开文档服务登记。平台负责通用标签和批量关闭，提供者只协调导图专属装配。
- `controller.py` 注册轻量清单激活后的菜单、面板、状态栏和偏好，不承担文档存储。

画布与面板的组合用例见 [Qt 编辑器集成测试](../qt-editor/tests/integration/)。
