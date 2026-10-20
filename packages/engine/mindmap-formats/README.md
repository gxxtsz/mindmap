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
