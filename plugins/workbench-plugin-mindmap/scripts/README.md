# 思维导图专项脚本

本目录只维护导图业务的性能和桌面验收。所有示例均从仓库根目录执行，兄弟脚本通过本目录导入。
需要读取 Git 历史或启动子进程的脚本使用仓库根目录，不把插件目录当成独立工作区。

| 脚本 | 验收内容 |
| --- | --- |
| `benchmark.py` | 千节点基础性能和测试文档生成 |
| `benchmark_incremental_layout.py` | 增量布局性能 |
| `benchmark_interactions.py` | 万节点交互、拖动与自动保存 |
| `benchmark_open.py` | 文件解析、首屏与打开耗时 |
| `benchmark_selection.py` | 大规模选择与面板刷新 |
| `branch_style_lab.py` | 分支样式与布局方向预览 |
| `verify_workbench.py` | 导图桌面功能、明暗主题与缩放 |
| `verify_submaps.py` | 子导图与详情页 |
| `verify_inspiration.py` | 灵感侧栏与独立想法 |
| `verify_branch_transfer_drag.py` | 分支跨区拖放 |
| `verify_node_drag.py` | 节点拖动、吸附与几何性能 |
| `verify_fold_controls.py` | 折叠控件 |
| `verify_text_rendering.py` | 文字渲染与历史版本对照 |

```powershell
uv run --locked --no-sync python plugins/workbench-plugin-mindmap/scripts/verify_workbench.py
uv run --locked --no-sync python plugins/workbench-plugin-mindmap/scripts/benchmark_interactions.py --nodes 10000
uv run --locked --no-sync python plugins/workbench-plugin-mindmap/scripts/verify_text_rendering.py --platform windows --baseline-ref HEAD
```

工作区构建与独立安装见[验收脚本说明](../../../scripts/README.md)。导图专项脚本随仓库提供。
