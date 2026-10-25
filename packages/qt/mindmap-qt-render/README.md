# mindmap-qt-render

思维导图的 Qt 测量、静态绘制与图形导出。本包可以独立构建和安装，不依赖桌面外壳或停靠框架。

要求 Python 3.12+、PySide6 `>=6.9,<7`。宿主先创建 `QApplication`，所有图形接口在 GUI 线程调用。

在仓库根目录执行 `uv build --package mindmap-qt-render` 构建本包；安装 wheel 时由元数据解析依赖。

## 独立导出

```python
from PySide6.QtWidgets import QApplication
from mindmap_core import MindMapDocument
from mindmap_layouts import create_default_layout_registry
from mindmap_qt_render import export_document

app = QApplication([])
document = MindMapDocument.new("独立渲染")
export_document(document, "导图.png", layout_registry=create_default_layout_registry())
```

同一接口支持 `.png`、`.svg`、`.pdf`；`scale` 控制 PNG 分辨率，`expand_all` 控制是否
展开全部节点。输出先在内存完成，再原子替换文件，失败不会截断旧目标。
离屏导出可在创建应用前设置 `QT_QPA_PLATFORM=offscreen`。

`VisualFactory` 维护文字及图片缓存，返回的 `NodeVisual` 只读共享。
`NodeItem` 是静态图元，不持有编辑宿主；交互节点和文本编辑器属于画布包。

## 字体渲染与多倍率验收

`VisualFactory` 保留节点选择的字族、字号、粗体、斜体及下划线，统一使用
`QFont.PreferAntialias` 和 `QFont.PreferNoHinting`，减轻非整数倍率下笔画过度贴合像素网格
造成的生硬边缘。标签、折叠计数和原地编辑沿用同一字体策略，系统字体回退及
ClearType 仍由 Qt 和操作系统处理，不修改宿主的全局字体或系统设置。

静态正文和原地编辑都启用 `QTextOption.setUseDesignMetrics(True)`，宽度测量仍使用相同
字体的 `QFontMetricsF`，使换行不依赖绘制时的像素取整。节点尺寸可能随新度量略有变化，
但改变画布倍率或设备像素比不会重新测量全图。视觉缓存保存字体和文本布局，不保存节点位图，
绘制时由 Qt 使用当前变换和设备像素密度生成字形；PNG、SVG、PDF 复用同一套视觉。

正文、标签、备注和折叠计数的独立绘制均显式开启文字抗锯齿；节点、文本及编辑器绘制
在正常完成、提前返回或异常时恢复调用方的绘图状态，避免影响相邻图元。

在仓库根目录运行下面的视觉验收命令。`--baseline-ref` 指向修改前的本地提交，省略该参数
时只生成当前版本的样例；输出写入 `.artifacts/text-rendering`，不读取用户导图或本机偏好。

```powershell
uv run --locked --all-packages python scripts/verify_text_rendering.py --platform windows --baseline-ref <修改前的提交哈希>
```

验收覆盖明暗主题，中文、英文、混排、数字标点、粗斜体、下划线、字体回退和 Markdown，
画布倍率为 10%、50%、75%、100%、115%、125%、150%、200%、400%，设备像素比为
1、1.25、1.5、2。每张对照图的样例以原始物理像素并排排列，不再缩放；同时生成四种
分辨率的 PNG 及保留矢量文字的 SVG、PDF，并记录布局及缓存检查结果。
隐藏画布还会在全部九个倍率下验证原地编辑几何、输入法候选隔离、确认、取消和撤销。
Windows 视觉验收使用原生 `windows` 平台；`offscreen` 可用于功能回归，字体渲染效果
可能因离屏字体库及后端不同而与桌面环境有差异。

## 节点与附页标记

纯图片节点按 `image_width` 与资源像素比例测量，不受图文附件的文本宽度及限高影响，图片边界就是节点边界。
`NodeVisual.image_only` 默认为 False，兼容旧构造；为 True 时只绘制图片及可选的临时选中轮廓，透明度原样保留，不绘制正文、背景、边框和附加标记。
尺寸手柄和拖动预览由画布视图前景提供，不写入本包缓存，不进入正式 PNG、SVG、PDF 导出。
节点正文不再为内部折叠按钮预留额外宽度，图片与标签按相同规则测量。
节点内容区上下各保留 8 个逻辑像素，多行文字、图片、标签和备注按实际内容撑高；
根节点、普通节点、原地编辑、拖动影像与图形导出共用相同的测量规则。
`NodeVisual.fold_rect(direction="right", child_count=0)` 返回独立的外侧按钮矩形，
支持右、左、下三种方向；零表示展开减号，正数用于测量完整的直接子节点计数。
`paint_fold_control()` 与 `paint(..., controls=True)` 保留静态绘制入口，圆框、符号和数字跟随节点边框。
画布用独立子图元及当前场景底色绘制外侧折叠按钮；正式 PNG、SVG、PDF 导出关闭折叠控件。

详情页与子导图标记位于节点内部右侧，按详情、子导图排列，正式导出及拖动影像保留这些静态标记。
每个按钮占 32×32、图形占 16×16，按钮间隔 2、正文到按钮区间隔 8、右侧留白 14；有入口时节点
最小高度为 36，单位均为 Qt 逻辑像素。`VisualFactory` 将页面存在状态纳入缓存键，测量时额外加入
按钮区，不改变原有正文换行宽度；图标和命中矩形均使用 `NodeVisual.page_control_rect(kind)`。
`NodeVisual.page_kinds` 仅保存页面类别，不持有页面 ID；`with_content_size(width, height)` 接收
不含按钮区的原内容尺寸并返回新的只读视觉，供原地编辑复用，保证长短草稿不会重复追加按钮留白。
`paint(..., page_controls=True)` 与 `NodeItem(..., page_controls=True)` 独立控制页面标记绘制，
默认开启，不改变原 `controls` 折叠标记开关；画布有独立页面按钮子图元时关闭正文中的重复绘制。
`page_icon_body(kind)` 提供 main、detail、submap 的共享 SVG 图形体，`paint_page_icon()` 在指定矩形
直接绘制矢量图形，节点、路径栏和图形导出复用同一份定义。新增视觉字段均有默认值，兼容既有构造调用。

`VisualFactory.visual()`、`measure()`、`update_measurements()` 和 `export_document()`
接受可选的 `detail_page_resolver`，可传入 `engine.resolve_detail_page`。解析函数接收节点 ID，
返回实际归属节点及详情页 ID；根节点和子导图入口据此展示共享详情，普通主题仍取自身引用。
省略参数保持原有行为；具体页面 ID 不进入视觉缓存，单页快照、编辑草稿、拖动影像和三种
图形导出可共用同一套图标几何。增量测量的变化节点列表必须包含共享详情变化影响的页内
别名，避免复用旧宽度。解析只读，不向节点快照写入借用的页面引用。

本包的运行依赖只有 `mindmap-core` 和 PySide6，不安装内置布局、格式或交互组件。
上述默认树布局示例需另行安装 `mindmap-layouts`；自定义宿主也可只实现核心 `LayoutAlgorithm`
并注册到 `LayoutRegistry` 后传入导出器。独立安装验收使用自定义布局验证三种图形输出。
