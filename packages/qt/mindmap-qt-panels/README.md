# mindmap-qt-panels

通过公开宿主协议连接的大纲、属性与设置面板。本包可以独立构建和安装，不依赖桌面外壳或停靠框架。

- **运行环境：** 要求 Python 3.12+、PySide6 `>=6.9,<7`。宿主先创建 `QApplication`，所有图形接口在 GUI 线程调用。

## 目录

- [安装与构建](#安装与构建)
- [宿主协议](#宿主协议)
- [面板组合与生命周期](#面板组合与生命周期)
- [大纲与节点属性](#大纲与节点属性)
- [导图设置](#导图设置)
- [独立灵感面板](#独立灵感面板)
- [独立收藏面板](#独立收藏面板)

## 安装与构建

在仓库根目录执行 `uv build --package mindmap-qt-panels` 构建本包；安装 wheel 时由元数据解析依赖。

## 宿主协议

- 公开入口包括 `MindMapPanels`、`OutlinePanel`、`NodePropertiesPanel`、`GlobalSettingsPanel` 和 `PanelHost`。

- 宿主提供协议中的文档、选择、布局校验和信号，不需要继承或安装 `MindMapWidget`；普通 QWidget 宿主也可以连接这些面板。

- `PanelHost` 源码详细说明参数、返回值、异常、借用数据和信号顺序。

- 面板只读使用 `document_view`、`parent_index`，不会访问宿主私有字段、场景或图元。

- 候选验证与正式事务分开，外部文档变化会取消过期输入，输入法候选保持原有提交语义。

- 仓库中的 `tests/test_host_protocol.py` 提供不使用画布的完整宿主实现与回归示例。

## 面板组合与生命周期

```python
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_panels import MindMapPanels

# 宿主应先创建 QApplication；此示例额外使用独立安装的画布包。
canvas = MindMapWidget()
panels = MindMapPanels(canvas)
# 将 panels.outline、panels.properties、panels.settings 放入宿主布局。
# 将此菜单直接加入宿主“设置”，滚轮标题会显示当前值。
wheel_menu = panels.settings.create_menu(canvas)
# 导图设置面板的显示由宿主侧边按钮或快捷键安排。
```

### 组合与显示

- `MindMapPanels(host, parent=None)` 生成 `outline`、`properties`、`settings` 三个业务对象。

- 三个面板均可由宿主放入布局，设置还通过 `settings.create_menu(parent)` 加入宿主菜单。

### 提交、输入法与释放

- 保存或关闭前调用 `flush_pending_edits()`；退出前调用 `dispose()`，再释放宿主。

- `is_composing` 只读汇总面板的输入法候选状态，保存或关闭前须等待候选完成；`close_popups()` 统一关闭大纲、属性、设置的菜单和补全弹层，不强制提交候选。

- 宿主通过这两个接口管理会话切换，无需依赖字段键名、具体控件或菜单对象。

- `set_ui_colors(UiColors | None)` 使用各面板的公开色板接口，传入 `None` 恢复文档主题。

- 释放后的 `is_composing` 返回 `False`，重复关闭弹层不再访问控件。

- 保存前调用 `flush_pending_edits()`；永久退出时先 `panels.dispose()`，再释放宿主或画布。

- 面板协调器不销毁 QWidget，最终控件所有权由宿主布局管理。

- `set_ui_colors(UiColors | None)` 只刷新展示，不提交草稿、不改写导图主题或历史。

## 大纲与节点属性

- **大纲层级：** `outline` 提供 `display_depth`、`set_display_depth()`、`display_depth_changed` 和 `depth_action`；None 表示所有层，1～9 表示指定层数，根主题计第1层。展开状态不修改导图折叠和历史。

- **属性提交：** `properties` 按字体、主题、分支分组，离散项立即提交，文本与数值等待400毫秒或失焦；只提交实际修改的字段，输入法候选不被保存、导出或外部刷新强制确认。

## 导图设置

### 布局与外观

- `settings` 侧栏分为“导图布局”和“导图外观”：前者提供布局结构与主题间距，后者提供整图连接线样式与导图主题。

- 布局入口以小图标和当前方向文字紧凑显示；连接线复用四列图形选择器，主题提供浅色、深色及只读的自定义状态。

- 外观字段标签在左、选择器靠右，窄栏自动换行并共用纵向滚动区；面板底部不显示说明标签，不支持的设置通过禁用状态和工具提示说明原因。

- `settings.create_menu(parent)` 保留调用签名，仅返回“滚轮行为：当前值”菜单，直接包含上下滚动和缩放两个互斥选项；菜单不再提供布局或外观入口。

- 面板显示由宿主的按钮或快捷键安排，不再提供 `layout_panel_requested` 信号；独立宿主无需安装桌面外壳。

### 间距与提交

- 主题间距预设为父子／同级的32／10、64／22、96／36，水平与竖直字段随向下布局转置显示。

- 数值输入等待400毫秒或回车、失焦后提交，宿主可调用 `settings.flush()` 提交，`discard_pending()` 取消，`composing` 查询候选状态。

- 组合的 `flush_pending_edits()`、`is_composing`、`dispose()` 已统一处理属性和布局输入，切换导图前先提交有效输入，再关闭弹层。

- 设置提交前请求有效属性输入，通过候选校验后写入一次事务；只读或文字编辑期间只允许调整滚轮。

- 查看面板或菜单只展示当前值，不自动更改自定义主题。

- 全局连接线选择会覆盖整张导图的局部样式；混合状态显示“多种样式”，重新选择同一种样式仍可统一全图。

- 设置对象不自行注册停靠窗口，面板及菜单位置均由宿主决定。

## 独立灵感面板

### 展示与选择

- `InspirationPanel(engine, parent=None)` 是独立的灵感单选侧栏，不扩大 `PanelHost` 协议，也不依赖画布或停靠包。

- 按游离根顺序展示标题与普通后代数量，提供标题搜索、新建入口；不提供多选整理、根排序或共同父主题菜单。

- 列表项参照收藏侧栏采用 58 逻辑像素的双行布局，左侧显示灵感灯泡，标题下方以次要文字色显示子主题数量；长文字在右侧省略，悬停提示保留完整内容。

- 当前想法由宿主同步的根标识决定高亮、标题加粗及图标强调色，悬停底色与键盘焦点轮廓独立显示，随界面色板更新。

- `navigate_requested(root_id)` 在列表单击或按 Enter 时请求打开想法，`new_requested` 请求新建；两者仅向宿主发送意图。

### 菜单与权限

- 列表右键菜单仅提供“加入导图…”和“删除想法”，中间保留分隔线，不设置默认动作；空白、空态和无结果区域右键仅提供新建，不会冒泡到宿主的停靠布局菜单。

- 鼠标按落点、键盘菜单按当前项解析目标，弹出或取消菜单不导航、不改变原有选择；目标按根标识区分，同名想法不会混淆。

- 文字编辑沿用画布入口。

- `attach_requested(root_id)`、`delete_requested(root_id)` 携带命中目标，宿主收到后复查权限并执行；面板不直接改写文档，也不借用画布当前选择。

- `menu_about_to_show` 在任一菜单弹出前同步发出，宿主应据此调用 `set_editable()` 更新输入法等即时权限。

### 动作与生命周期

- 文档发生内容修改或替换时自动关闭菜单，保存完成事件保留菜单；宿主切换会话时须调用 `close_popups()` 关闭两个菜单并清除目标，释放时由 `dispose()` 同样处理，拒绝迟到动作。

- `new_action` 由面板持有，供宿主标题栏代理使用；空列表时显示共用该动作的“新建想法”文字按钮，已有想法但搜索无匹配时只显示无结果提示。

- 搜索框位于内容区顶部，不再单独保留新建工具行。

- 动作随 `set_editable()` 同步权限，随 `set_ui_colors()` 更新加号图标；宿主不能转移其所有权或在释放后触发。

### 拖放与反馈

- 宿主通过 `set_current_root_id()`、`set_editable()`、`set_ui_colors()` 同步展示，通过 `set_drag_handlers(create, accept, drop)` 注入只读验收及正式拖放回调。

- 拖起列表项不触发导航，画布分支放入侧栏时统一成为独立想法，不作为列表项的子主题。

- 列表拖入当前画布时，宿主复用画布内部的节点拖动控制器，支持接为子主题、同级插入及一致的预览和动画；面板仍只提供标识载荷，不计算落点或改写样式。

- 原生拖放结束后发出 `drag_finished`，宿主通过 `cancel_branch_transfer_preview()` 清理未提交预览，已提交的重排动画继续播放。

- 底部拖放反馈默认隐藏且不占位，仅在合法分支拖入时显示；离开、取消或拒绝拖放时隐藏。

- `flash_roots(node_ids)` 仅提示成功新增的数量，1.6 秒后隐藏，不改变画布或搜索；新的拖放交互会取消旧成功提示的倒计时，避免当前反馈被提前关闭。

- 所有方法在 GUI 线程调用；引擎在面板存活期间必须有效，永久释放前先 `dispose()`。

## 独立收藏面板

### 创建与职责

- `SubmapFavoritesPanel(engine, parent=None)` 为一份完整文档提供子导图收藏列表，不依赖画布或停靠框架，也不增加既有 `PanelHost` 的必需成员；原 `MindMapPanels` 组合保持三个面板，收藏面板由支持页面导航的宿主单独创建、安装和释放。

### 导航与取消收藏

- 面板只读订阅引擎，按收藏顺序显示实时名称和页面层级路径；单击或 Enter 发出 `navigate_requested(page_id)`，右键取消发出 `unfavorite_requested(page_id)`。

- 宿主应分别连接安全的页面导航与取消收藏接口，由宿主检查只读和输入法状态、提交有效草稿。

- 控件自身不写入文档、不切页，也不建立全文件快照。

### 展示同步与释放

- `set_current_page_id(page_id)` 同步当前页高亮，`set_editable(bool)` 控制取消收藏入口，禁用修改时仍可浏览、导航。

- `set_ui_colors(UiColors | None)` 同步列表及菜单主题，None 恢复默认浅色；这些方法均不触发内容修改。

- 会话离开时调用 `close_popups()` 关闭菜单并清除待操作目标；永久退出前调用幂等 `dispose()` 解除引擎订阅，随后由宿主销毁控件，借用的引擎必须至少保持有效到释放完成。
