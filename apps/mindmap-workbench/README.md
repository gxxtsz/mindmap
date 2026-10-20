# 思维导图工作台

基于 Harbor 的单文档桌面应用，画布、大纲、属性和导图设置复用 `mindmap-qt`。
支持 Python 3.12、3.13，当前工作区锁定 PySide6 6.12.0。

开发时先将 [harbor-workbench](https://github.com/gxxtsz/harbor-workbench) 放在本仓库同级目录，
再在本仓库根目录运行：

```powershell
uv sync --all-packages --group dev
uv run --locked --package mindmap-workbench mindmap-workbench
```

命令末尾可追加待打开的导图或可导入文件路径。其他启动参数如下：

| 参数 | 行为 |
| --- | --- |
| `--theme light|dark` | 设置外壳主题，优先于已保存的外壳主题 |
| `--state-dir 目录` | 使用独立的本机配置目录 |
| `--no-restore` | 跳过 Harbor 会话布局恢复；滚轮偏好仍从本机配置读取 |
| `--compact` | 启用 Harbor 外壳紧凑模式；导图紧凑布局由“导图设置”管理 |
| `--size 宽x高` | 指定逻辑像素尺寸，默认 1440x900，范围为 860x540 至 8000x8000 |
| `--screenshot 文件.png` | 保存窗口截图及同名 JSON 元数据后退出；导图图形导出使用应用中的导出操作 |

始终只有一个导图会话，默认启动空白导图；新建和打开替换当前文档。
本机会话恢复只恢复外壳布局与偏好，不自动重开上次的导图文件。
中央标签关闭后保留画布与撤销历史，通过“视图 → 显示导图”复用原实例。
保存、导出和关闭前提交有效属性输入与原地文字；取消关闭后仍可继续编辑。

大纲、节点属性、导图设置依次使用 Alt+1、Alt+2、Alt+3，默认仅显示右侧节点属性。
F6 返回画布，Escape 取消原地文字编辑或退出分支聚焦。

Harbor 的应用主题与导图主题独立，窗口布局和滚轮偏好不进入导图文件。
本机布局与偏好位于 Harbor 的 `mindmap-workbench` 配置目录；
其中 `preferences.ini` 保存滚轮偏好。导图主题影响画布、节点、连接线和 PNG/SVG/PDF 导出。

开发期对 Harbor 的两个包使用可编辑依赖，普通源码更新后重启即可使用，包元数据更新后需要重新同步。
正式交付使用普通 wheel，不依赖相邻源码目录；框架更新后仍需重新构建和验收交付包。
检查与构建步骤见[仓库说明](../../README.md)。
