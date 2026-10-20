# 原生思维导图

使用 Python、PySide6 与 uv workspace 构建的思维导图引擎及单文档桌面工作台。
工作台支持 Python 3.12、3.13；当前工作区使用 Python 3.12，锁定 PySide6 6.12.0。
各引擎包的 Python 要求为 3.12+，不依赖 Qt 或桌面框架。

## 快速开始

克隆本仓库后直接在根目录执行，全部基础包均包含在当前工作区：

```powershell
uv sync --locked --all-packages --group dev
uv run --locked --package mindmap-workbench mindmap-workbench
```

也可在启动时打开指定文件：

```powershell
uv run --locked --package mindmap-workbench mindmap-workbench 我的导图.mindmap
```

各成员通过 workspace 引用本仓库源码，普通源码更新后重启工作台即可使用；依赖或包元数据更新后重新同步。
工作台支持 `--theme light|dark`、`--state-dir`、`--no-restore`、`--compact`、
`--size` 和 `--screenshot`，详细用法可执行
`uv run --locked --package mindmap-workbench mindmap-workbench --help` 查看。

## 当前功能

支持节点编辑、11 种分支连接线、四方向树布局、图片、复制粘贴、撤销重做和 PNG/SVG/PDF 导出。
桌面工作区提供可拖动的工具窗口按钮，大纲、节点属性和导图设置可停靠到左、右、底部或浮动。
界面采用中性外围、独立圆角卡片和窄间隙，菜单、侧栏、浮动面板及应用自有弹窗统一适配明暗主题。
外壳主题与导图主题独立；画布仍使用导图的背景色，界面装饰不进入 PNG、SVG、PDF 导出。
属性侧栏自动提交，支持多选样式及分支批量编辑；面板位置、尺寸和展开状态保存在本机。
节点属性按字体、主题、分支纵向分组，无 Tab 或内容编辑字段；支持字体搜索、色块选择及自定义最大文本宽度。
节点属性的“分支”分组提供四列图形选择器，可选择“整个子树”或“仅本级”；未设置样式的节点保留原有曲线。
导图设置通过 Alt+3 或工具窗口菜单打开，集中提供滚轮、布局方向、紧凑布局、整图分支样式和导图主题切换。
全局分支设置覆盖整张导图，后续新增节点沿用父节点样式；节点属性仍可单独设置局部分支。
中央文档标签栏右侧提供撤销、重做，画布右下角浮条提供适应窗口和回到中心，不再单独占用一行工具栏。
这四个入口复用原动作、快捷键和启用状态，浮条随窗口调整位置并适配明暗主题，不进入图形导出。
通过侧边按钮、编辑菜单或 Alt+2 打开属性侧栏。
画布隐藏两条滚动条，随可见节点和图片自动扩展、收缩，四周各保留一屏操作余量。

始终使用一个文档会话，新建和打开替换当前导图；中央标签关闭后可通过“视图 → 显示导图”恢复。
大纲、节点属性和导图设置分别使用 Alt+1、Alt+2、Alt+3，默认仅展开右侧节点属性。
布局和滚轮偏好只保存在本机，应用配置命名空间为 `mindmap-workbench`。

布局与文档格式支持实例级显式注册，工作台的算法菜单与文件筛选项根据能力自动生成。
当前文件格式为版本 2，只读取当前版本，不提供旧数据迁移。

## 工作区

源码按子系统分组，四个无界面引擎包统一位于 `packages/engine/`：

```text
packages/
├─ engine/
│  ├─ mindmap-core/
│  ├─ mindmap-layouts/
│  ├─ mindmap-formats/
│  └─ mindmap-engine/
├─ mindmap-qt/
├─ mindmap-docking/
└─ mindmap-shell/
apps/
└─ mindmap-workbench/
tests/
└─ desktop/
```

`engine/` 仅用于目录分组，四个引擎成员各自保留 `pyproject.toml`、`src/`、`tests/` 和许可证。
桌面基础包的组合回归集中在 `tests/desktop/`；八个成员均可独立构建。

| 成员 | 导入名 | 职责 |
| --- | --- | --- |
| mindmap-core | mindmap_core | 模型、编辑、事务、历史、事件、几何及扩展协议，无项目内依赖 |
| mindmap-layouts | mindmap_layouts | 内置树布局和默认布局注册表，只依赖核心 |
| mindmap-formats | mindmap_formats | 原生容器、JSON、大纲和原子文件读写，只依赖核心 |
| mindmap-engine | mindmap_engine | 默认组合入口，重新导出上述三个包的公开接口，无 Qt 依赖 |
| mindmap-qt | mindmap_qt | 原生画布、输入、图片及图形导出 |
| mindmap-docking | mindmap_docking | 停靠面板、文档标签、分屏、主题与布局管理 |
| mindmap-shell | mindmap_shell | 桌面外壳、应用动作、菜单和启动流程 |
| mindmap-workbench | mindmap_workbench | 单文档思维导图桌面应用 |

工作区共用根目录的 `.venv` 和 `uv.lock`。成员各自声明运行依赖，通过
`tool.uv.sources` 的 `workspace = true` 引用本地成员。

## 检查与构建

```powershell
uv run --locked --all-packages pytest
uv run --locked --all-packages ruff check .
uv build --all-packages
uv run --locked --all-packages python scripts/verify_wheels.py
uv run --locked --all-packages python scripts/verify_workbench.py
```

八个成员全部在当前仓库构建，wheel 验收仅使用本仓库 `dist/` 中的构建结果，
在仓库外的干净环境中验证依赖隔离和工作台完整流程。
安装报告与固定版本约束写入 `.artifacts/`，视觉验收结果写入 `.artifacts/workbench-visual/`。
视觉验收检查 100%、150%、200% 三档缩放；Windows 默认使用原生 `windows` 平台，
其他系统默认使用 `offscreen`。Windows 也可传入 `--platform offscreen` 执行离屏检查。

`mindmap-qt` 的独立画布、面板组合、文件控制器和嵌入工作区接口见
[组件说明](packages/mindmap-qt/README.md)，它们不依赖桌面框架。
[桌面框架说明](docs/桌面框架.md)介绍两个基础包的接口、依赖方向和应用配置；
代码与资源许可见[资源与许可](docs/资源与许可.md)。
`scripts/branch_style_lab.py` 直接调用正式路由及绘制代码，输出四种布局方向的样式验收图；
`scripts/benchmark.py` 用于千节点性能验收。
