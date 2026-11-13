# 思维导图

基于 Python、PySide6 和 uv workspace 的本地思维导图应用。思维导图是唯一主体，普通详情页、子导图和节点专用详情页共同组织内容。软件架构是节点的一种专用详情页，后续功能通过相同页面协议接入。

## 启动

支持 Python 3.12 和 3.13。首次使用先克隆仓库：

```powershell
git clone https://github.com/gxxtsz/mindmap.git
cd mindmap
```

在仓库根目录安装依赖并启动：

```powershell
uv sync --locked --all-packages --group dev
uv run --locked --package mindmap-app mindmap
```

也可运行 `uv run --locked --package mindmap-app python -m mindmap_app`。

| 参数 | 用途 |
| --- | --- |
| 文件路径 | 打开 `.mindmap` 导图 |
| `--theme light\|dark` | 指定主题 |
| `--state-dir 目录` | 使用独立配置目录 |
| `--no-restore` | 跳过上次标签和布局恢复 |
| `--compact` | 使用紧凑外壳 |
| `--size 宽x高` | 指定窗口尺寸 |
| `--screenshot 文件.png` | 保存窗口截图和元数据后退出 |

## 节点扩展

1. 新建或打开导图，选择文字节点。
2. 使用“扩展详情…”添加软件架构。
3. 在页面内导入 `.software-structure` 数据，或下载格式模板。
4. 浏览模块依赖、接口和筛选，使用页面返回按钮回到原节点。

拥有扩展的节点显示可点击的页面图标。每个节点可拥有多种扩展，每种类型一页；普通详情与专用详情可以共存。

扩展数据全部进入 `.mindmap`，与节点共享自动保存、撤销重做、多标签同步及复制删除。扩展视口和最近访问属于本机会话。未知扩展或业务版本仍保留数据，不影响其他节点编辑。

## 代码结构

```text
apps/mindmap-app/                  产品启动、直接装配、历史数据迁移
mindmap/
  core/                           节点模型、事务、布局、历史、原生格式
  qt-render/                      静态绘制、节点测量、图形导出
  qt-editor/                      导图画布、面板、节点页面协议与容器
  application/                    导图应用、文档会话、导航、自动保存
extensions/software-structure/    软件架构节点详情页
packages/
  mindmap-desktop-api/             导图应用与桌面的内部协议
  mindmap-desktop-runtime/         窗口、标签、分屏、配置、日志基础设施
```

8 个分发包共用一个工作区和锁文件。产品直接构造导图应用，不提供顶级业务发现或启停机制。外部能力只通过 `mindmap.node_pages` 注册节点专用详情页。

## 保存与会话

原生 `.mindmap` 使用 ZIP 容器，读取结构版本 2 至 7；有扩展页时写为版本 7。没有扩展页时仍使用对应的版本 5 或 6。

本机配置位于系统配置目录的 `mindmap` 下：`mindmap-state-v2.json` 保存导图标签、导航与分屏，`mindmap-settings-v1.json` 保存窗口和导图偏好。旧配置由[历史数据迁移](docs/历史数据迁移.md)单向导入，原文件保留。

## 验证与构建

```powershell
uv run --locked --no-sync pytest
uv run --locked --no-sync ruff check .
uv lock --check
uv build --all-packages
```

## 开发说明

- [架构与节点扩展](docs/架构与节点扩展.md)
- [导图组件](mindmap/README.md)
- [导图应用层](mindmap/application/README.md)
- [软件架构详情页与导入格式](extensions/software-structure/README.md)
- [桌面内部协议](packages/mindmap-desktop-api/README.md)
