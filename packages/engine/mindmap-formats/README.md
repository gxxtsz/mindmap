# mindmap-formats

文档格式能力包，仅依赖 `mindmap-core`，不需要安装任何布局实现。

内置 `.mindmap`、JSON、Markdown 大纲和两空格缩进文本。原生容器与 JSON 完整保留文档，
大纲仅保留层级和文字，属于有损交换格式。当前格式版本为 2，不提供旧版本兼容或迁移。
Markdown 导入支持标题和列表条目，不解析完整 Markdown 文档语义；大纲导出将节点正文中的换行合并为空格。

```python
from mindmap_core import MindMapDocument
from mindmap_formats import create_default_format_registry, load_document, save_document

registry = create_default_format_registry()
document = MindMapDocument.new("中心主题")
save_document(document, "导图.mindmap", registry=registry)
restored = load_document("导图.mindmap", registry=registry)
save_document(restored, "大纲.md", registry=registry)
```

读写函数接受 `registry`、`format_id` 和 `options` 关键字参数。显式格式标识优先，
后缀有多个匹配时要求明确指定。所有导出复用原子文件写入，失败保持已有目标不变。
`encode_document` 和 `decode_document` 提供内存流入口；`dumps`、`loads` 提供 JSON 文本入口。
文件服务不修改任何引擎保存标记，宿主应在主文档保存成功后调用 `mark_saved()`。
上述是底层通用文件与内存交换能力；桌面工作台及 Qt 文件控制器的打开、保存和另存为只允许 `.mindmap`，不提供其他文件格式入口。
JSON、Markdown 和文本编解码器仍用于剪贴板，PNG、SVG、PDF 图形导出由画布独立处理。

## 亿图脑图剪贴板

`import_edraw_xml(data: bytes) -> MindMapDocument` 解析亿图普通脑图的
`application/edraw-mind-xml` XML 载荷，返回带合成根的多树片段。
来源节点的 `LevelData/SubLevel` 决定子节点顺序，`Super` 补充父引用；未复制的父节点不会凭空补出。
普通主题、子主题与浮动主题只保留纯文本和层级，支持 `pp/tp` 段落及 `HtmlText` 的换行；
连接线、概要、图片、字体、颜色、备注、链接及源节点标识均不导入。

本接口不注册到文件格式列表，不读取本机文件，也不依赖 Qt。
亿图批量复制产生的绝对 `copy.xml` 路径由 Qt 层限量读取后传入。
公开的 `MAX_ARCHIVE_BYTES` 为所有文档与剪贴板读取共用的容量上限。
结构损坏、循环、多父节点或重复标识抛出中文 `MindMapError`，不修补或猜测损坏的树。
