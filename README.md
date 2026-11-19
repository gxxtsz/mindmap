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
| 目录路径 | 打开包含 `mindmap.json` 的导图目录 |
| `--theme light\|dark` | 指定主题 |
| `--state-dir 目录` | 使用独立配置目录 |
| `--no-restore` | 跳过上次标签和布局恢复 |
| `--compact` | 使用紧凑外壳 |
| `--size 宽x高` | 指定窗口尺寸 |
| `--screenshot 文件.png` | 保存窗口截图和元数据后退出 |

## 节点文字与宽度

- 导图和节点图中新建的文字节点默认渲染 Markdown。双击正文或按 F2 编辑源码，按 `Shift+Enter` 换行。右键可切换「显示为纯文本」或「渲染为 Markdown」。已有节点按保存的格式显示。
- 编辑 Markdown 节点时，从网页粘贴的标题、粗体、斜体和列表会转成 Markdown 源码，确认后按样式显示。
- 单选文字节点，沿水平方向拖动右下角手柄。拖动时可查看新的换行和高度，松手后更新布局。右键选择「恢复自动宽度」会按内容重新测量。
- 格式和固定宽度随导图保存，也可以撤销或重做。PNG、SVG、PDF 使用已提交的节点排版。

## 节点图

1. 选中文字节点，从编辑菜单或节点右键菜单选择「添加图」。已有图时可以选择「打开图」或点击节点上的图标。
2. 在图页按 Tab 或 Enter 添加节点，或从右键菜单选择「编辑图文本…」粘贴整张图。拖动节点可调整位置，双击或按 F2 可编辑标题。
3. 在图页空白处或节点上右键，选择「DOT 自动布局」重排整张图。选中节点会高亮关联连线，Ctrl 点击或 Shift 拖框可批量选线，再按 Delete 删除。
4. 点击路径栏的返回按钮回到所属节点。图随导图目录自动保存，可以撤销、重做和在多个标签中同步编辑。

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

「打开导图」选择导图目录，也可以将目录拖进窗口，或把目录路径附在启动命令末尾。「新建」和「另存为」先选择父目录，再输入导图目录名。

- `mindmap.json` 保存版本、主导图入口、页面索引、全局设置和图片清单。
- `pages/` 为主图、灵感区、子导图和详情页各保存一个 JSON。图节点的数据保存在所属页面。
- `assets/` 按 SHA-256 保存图片原始字节，没有图片时可省略此目录。

可以用 Git 跟踪上述内容。应用只替换有变化的文件。若目录作为 Git 工作区使用，可在其根目录将 JSON 的换行固定为 LF，并让 Git 按二进制跟踪图片。在应用关闭导图后切换分支，可以让应用下次直接读取该分支的内容。编辑期间若发现磁盘变化，「文件」菜单可重新加载磁盘内容或将本地修改另存为。

```gitattributes
/mindmap.json text eol=lf
/pages/*.json text eol=lf
/assets/* binary
```

本机配置位于系统配置目录的 `mindmap` 下。`mindmap-state-v2.json` 保存导图标签、导航与分屏，`mindmap-settings-v1.json` 保存窗口和导图偏好。

## Git 同步

安装 Git，并让导图目录本身成为 Git 仓库、当前分支跟踪远程上游。打开导图后，顶部「Git 同步」显示待保存、未提交、待推送和远程更新情况。保存后立即检查本地；外部 Git 操作约两秒内反映，远程每 30 秒检查一次，也可点击「立即检查」。后台检查不会更新工作区、创建提交或推送。

- **更新导图**：本地工作区干净、分支只落后于远程时可用。更新成功后，各标签回到原来的页面和视口；页面已经删除时回到主图。
- **提交并推送**：保存编辑，用「更新导图」作为提交说明，仅提交导图清单、页面和图片，然后推送至当前分支的上游。其他文件的暂存内容保持原样。推送失败时保留本地提交，可再次点击「推送」。
- **仅提交**：只在本地留下提交。两端都有修改时可以先用此操作保留本地版本，再在 Git 中处理分叉，处理后重新检查。应用不会自动合并或覆盖。

远程不可用时仍可本地编辑、保存和提交。应用使用系统已配置的 Git 凭据；如遇认证失败，请先在终端确认该仓库的 Git 拉取权限。若 Git 已更新但导图加载失败，可在「文件 → 处理保存冲突」重新加载磁盘版本或将当前导图另存为。

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
