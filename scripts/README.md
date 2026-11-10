# 工作区与产品验收脚本

本目录保存跨包开发工具，均从仓库根目录执行。

| 脚本 | 职责 |
| --- | --- |
| `workspace_members.py` | 按根配置发现真实成员，不通过包名猜测目录 |
| `verify_wheels.py` | 比较源码与发行内容，验证十二个包独立安装、宿主桌面资源和仅安装文本插件的组合 |
| `verify_plugins.py` | 验证产品明暗主题、混合分屏、中文组合输入、插件管理及重启恢复 |

```powershell
uv build --all-packages
uv run --locked --no-sync python scripts/verify_wheels.py
uv run --locked --no-sync python scripts/verify_plugins.py
```

导图性能、节点拖动、页面和渲染专项验收归属[导图插件脚本](../plugins/workbench-plugin-mindmap/scripts/README.md)。
产物统一写入仓库 `.artifacts/`，测试使用临时文档和配置，不读取用户的默认会话。
