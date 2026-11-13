# 思维导图

基于 Python、PySide6 和 uv 工作区的本地思维导图应用。可以用树组织主题，在任意文字节点上挂载一张有向图。

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

## 节点图

1. 选中文字节点，从编辑菜单或节点右键菜单选择「添加图」。已有图时可以选择「打开图」或点击节点上的图标。
2. 在图页按 Tab 或 Enter 添加节点，或从右键菜单选择「编辑图文本…」粘贴整张图。拖动节点可调整位置，双击或按 F2 可编辑标题。
3. 从右侧停靠栏打开「图页面设置」，可用「DOT 自动布局」重排整张图。选中节点会高亮关联连线，Ctrl 点击或 Shift 拖框可批量选线，再按 Delete 删除。
4. 点击路径栏的返回按钮回到所属节点。图随 `.mindmap` 自动保存，可以撤销、重做和在多个标签中同步编辑。

文本语法和操作示例见[节点图使用说明](docs/节点图.md)。

## 代码结构

```text
apps/mindmap-app/                  产品启动、直接装配、历史数据迁移
mindmap/
  core/                           节点模型、事务、布局、历史、原生格式
  qt-render/                      静态绘制、节点测量、图形导出
  qt-editor/                      导图画布、面板、节点页面协议与容器
  application/                    导图应用、文档会话、导航、自动保存
packages/
  mindmap-desktop-api/             导图应用与桌面的内部协议
  mindmap-desktop-runtime/         窗口、标签、分屏、配置、日志基础设施
```

7 个分发包共用一个工作区和锁文件。产品入口直接构造导图应用和桌面运行时。

## 保存与会话

原生 `.mindmap` 使用 ZIP 容器，可读取结构版本 2 至 7。有节点图或专用详情页的文件使用版本 7。

本机配置位于系统配置目录的 `mindmap` 下。`mindmap-state-v2.json` 保存导图标签、导航与分屏，`mindmap-settings-v1.json` 保存窗口和导图偏好。

## 验证与构建

```powershell
uv run --locked --no-sync pytest
uv run --locked --no-sync ruff check .
uv lock --check
uv build --all-packages
```

## 开发说明

- [导图组件](mindmap/README.md)
- [导图应用层](mindmap/application/README.md)
- [桌面内部协议](packages/mindmap-desktop-api/README.md)
