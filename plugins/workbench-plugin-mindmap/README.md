# 思维导图插件

通过公开工作台 API 提供 `.mindmap` 编辑器、业务面板、页面历史、共享视图和自动保存。
全部导图专属源码、组件测试、组合测试、性能脚本及使用文档归属本目录。

## 目录与分发边界

| 目录 | 内容 |
| --- | --- |
| [adapter](adapter/README.md) | `workbench-plugin-mindmap` 适配包：插件注册、文档、视图、面板装配 |
| `engine/` | `mindmap-core`、`mindmap-layouts`、`mindmap-formats`，纯 Python 业务组件 |
| `qt/` | `mindmap-qt-render`、`mindmap-qt-ui`、`mindmap-qt-canvas`、`mindmap-qt-panels` |
| `tests/integration/` | 引擎、格式、画布与面板的业务组合验收 |
| [scripts](scripts/README.md) | 导图性能基准、绘制和桌面专项验收 |
| `docs/` | 使用指南、节点编辑与页面功能说明 |

目录归属与分发边界分别维护：本目录不设置 `pyproject.toml`；八个分发包分别注册到根 uv workspace。
从任意成员目录均使用根锁文件及环境。各成员的 `src/`、`tests/`、许可证和构建元数据保持独立。
导图格式、业务包名及 `mindmap_core`、`mindmap_qt_*` 导入路径保持稳定。

## 运行

在仓库根目录执行：

```powershell
uv sync --locked --all-packages --group dev
uv run --locked --package personal-workbench personal-workbench
```

## 文档

- [使用指南](docs/使用指南.md)：多标签、共享文档、保存、面板、日志与偏好。
- [编辑与页面](docs/编辑与页面.md)：节点、图片、子导图、详情页、引用与灵感。
- 公开协议、注册、生命周期及兼容策略见[工作台运行时](../../packages/workbench-runtime/README.md)。
- [开发与验收](../../docs/开发与验收.md)：工作区、构建和性能验收命令。
