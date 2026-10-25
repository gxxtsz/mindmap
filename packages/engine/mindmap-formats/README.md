# mindmap-formats

文档格式能力包，仅依赖 `mindmap-core`，不需要安装任何布局实现。

## 目录

- [格式与版本](#格式与版本)
- [文件与内存读写](#文件与内存读写)
- [缩进大纲剪贴板](#缩进大纲剪贴板)
- [亿图脑图剪贴板](#亿图脑图剪贴板)
- [独立进程保存能力](#独立进程保存能力)

## 格式与版本

### 原生结构与兼容

- 原生容器及 JSON 写入版本 5，使用独立灵感区记录、扁平子导图表、详情页表及全文件节点、图片资源表，支持自由摆放的多棵游离树、深层子导图及单层详情，不产生递归 JSON 对象。

- 兼容版本 2、3、4，缺少灵感区的旧文件视为空区。

- 版本 1 及未知版本仍明确拒绝；读取不修改原文件，后续保存使用版本 5。

- 页面归属、子导图叶子约束、详情页禁止嵌套、名称同步、循环和资源引用均在导入时校验。

- 原生剪贴板保留两类附属页面，Markdown 和文本仍只交换页内分支。

### 交换格式

- 内置 `.mindmap`、JSON、Markdown 大纲和两空格缩进文本。

- 原生容器与 JSON 完整保留文档，大纲仅保留层级和文字，属于有损交换格式。

- 当前原生结构版本为 5，兼容读取版本 2、3、4。

- Markdown 导入支持标题和列表条目，不解析完整 Markdown 文档语义；大纲导出将节点正文中的换行合并为空格。

## 文件与内存读写

```python
from mindmap_core import MindMapDocument
from mindmap_formats import create_default_format_registry, load_document, save_document

registry = create_default_format_registry()
document = MindMapDocument.new("中心主题")
save_document(document, "导图.mindmap", registry=registry)
restored = load_document("导图.mindmap", registry=registry)
save_document(restored, "大纲.md", registry=registry)
```

### 调用约定

- 读写函数接受 `registry`、`format_id` 和 `options` 关键字参数。

- 显式格式标识优先，后缀有多个匹配时要求明确指定。

- 所有导出复用原子文件写入，失败保持已有目标不变。

- `encode_document` 和 `decode_document` 提供内存流入口；`dumps`、`loads` 提供 JSON 文本入口。

- 文件服务不修改任何引擎保存标记，宿主应在主文档保存成功后调用 `mark_saved()`。

### 桌面文件边界

- 上述是底层通用文件与内存交换能力；桌面工作台及 Qt 文件控制器的打开、保存和另存为只允许 `.mindmap`，不提供其他文件格式入口。

- JSON、Markdown 和文本编解码器仍用于剪贴板，PNG、SVG、PDF 图形导出由画布独立处理。

## 缩进大纲剪贴板

### 片段接口与示例

- `import_indented_fragment(text: str) -> MindMapDocument` 将一行一个节点的纯文本解析为多根片段，支持 Tab 或每级 2／4 空格；只保留文字、层级及同级顺序，不解释 Markdown或 ASCII 树状连接线。

- 返回的「剪贴板」合成根仅作为片段容器，`root.children` 为全部顶层节点，可以直接传给引擎的 `paste_fragment`，不会额外粘贴可见容器。

例如下列内容产生两棵并列子树：

```text
开发计划
    前端
        页面设计
    后端
测试计划
    功能测试
```

### 缩进与错误处理

- 一个 Tab 始终表示一级。

- 解析器分别尝试 2／4 空格单位，将行首 Tab 换算为对应单位的空格后，减去所有非空行的最小缩进作为公共基线。

- 两种解释都遵守首次从顶层开始、每次最多增加一级的规则，不补建缺失父节点。

- 唯一合法解释或相同树形均接受；两种解释都合法但树形不同时，报告首个歧义行。

- 例如“甲”、Tab 缩进的“乙”、四空格缩进的“丙”会在第 3 行报歧义，需统一缩进方式。

- 忽略 BOM、空行和行尾空白，保留正文内部的空格、Tab 及符号。

- 异常抛出中文 `MindMapError`，行号从原文第 1 行开始并包含空行；两种解释均失败时，报告合法前缀最长者的首个错误，并列采用 2 空格解释。

- 空白内容提示“大纲内容不能为空”。

- 函数完整校验后才建立新节点，不修改宿主文档、剪贴板、文件或撤销历史。

### 统一流入口

也可通过统一流入口使用片段模式：

```python
from mindmap_formats import decode_document

fragment = decode_document(
    "开发计划\n    前端\n测试计划".encode("utf-8"),
    format_id="text",
    options={"as_fragment": True},
)
```

- `as_fragment` 是纯文本导入专用布尔选项，默认 `False`；不传时继续使用原有每两空格一级、首行为根的单根文档约定。

- Markdown 仍使用既有标题／列表导入语义，缩进或层级错误同样包含原文行号。

- 现有文本导出保持两空格缩进，文件结构版本不变。

### 格式识别与宿主交互

- `detect_outline_format(text: str) -> str` 以首个非空行是否符合本包的标题、无序列表（`-`、`*`、`+`）或有序列表（`1.`、`1)`）语法，返回 `markdown` 或 `text`，忽略开头BOM。

- 普通 `#标签`、`-正文` 不触发 Markdown；格式确定后的解析错误不回退为其他格式。

- 桌面画布在完整解析后显示对应来源的粘贴配置，与亿图共用追加／替换及根节点、原有后代选项，并共享上次成功的单根选择；这些交互与本机偏好由画布和工作台处理，不进入格式解析接口或导图文件。

## 亿图脑图剪贴板

### 解析内容

- `import_edraw_xml(data: bytes) -> MindMapDocument` 解析亿图普通脑图的 `application/edraw-mind-xml` XML 载荷，返回带合成根的多树片段。

- 来源节点的 `LevelData/SubLevel` 决定子节点顺序，`Super` 补充父引用；未复制的父节点不会凭空补出。

- 普通主题、子主题与浮动主题只保留纯文本和层级，支持 `pp/tp` 段落及 `HtmlText` 的换行；连接线、概要、图片、字体、颜色、备注、链接及源节点标识均不导入。

### 调用边界与异常

- 本接口不注册到文件格式列表，不读取本机文件，也不依赖 Qt。

- 亿图批量复制产生的绝对 `copy.xml` 路径由 Qt 层限量读取后传入。

- 公开的 `MAX_ARCHIVE_BYTES` 为所有文档与剪贴板读取共用的容量上限。

- 结构损坏、循环、多父节点或重复标识抛出中文 `MindMapError`，不修补或猜测损坏的树。

- 原生容器及 JSON 解码直接接管内部解析对象，避免整份 JSON 邻接表再次深拷贝；公开 `from_dict()` 仍完整隔离调用方的嵌套字典和列表。

- `supports_owned_load(registry)` 只读判断原生读取链是否为未覆写的内置实现。只有刚读取且尚未发布的独占结果可以交给引擎 `adopt_document()`；扩展注册表、编解码器或覆写入口继续使用 `replace_document()` 的隔离副本。此能力判断不会跳过结构、资源或画布校验。

- `load_validated_document(path, registry=None) -> ValidatedDocument` 只用于标准原生打开链路，返回独占文档的一次性完整校验凭据。容器、容量、JSON 重复字段、全部页面结构及资源哈希校验保持不变，同一结果不再被注册表重复遍历；扩展读取器在读盘前被拒绝，应改用 `load_document()`。

- 宿主只读预检 `receipt.document` 后，可通过 `engine.adopt_document(receipt.document, validation=receipt)` 接管。凭据不能用于其他对象或修改过的文档；接管成功即失效，取消和错误均不改写源文件。接口不依赖 Qt，可在工作线程独占准备，再将凭据移交引擎所属线程。

## 独立进程保存能力

### 编码链能力

- `supports_process_save(registry, format_id="mindmap")` 只读查询当前编码链能否在独立进程中重建。

- 当前仅支持标准注册表和未覆写的内置原生编码器；缺失、单向、自定义或被替换的调度入口返回 `False`，宿主应沿用同步保存，不绕过扩展实现。

### 快照与调度

- 捕获引擎快照后，再使用 `can_process_snapshot(document)` 检查标准实体及附加状态。

- 扩展文档、节点、资源或节点动态属性会回退同步处理，不删除扩展状态、不尝试传输GUI 对象。

- 上述查询不复制文档、不写文件，也不替代编码时的完整校验。

- 任务调度、防抖、文件归属及完成后标记保存版本仍由宿主管理。
