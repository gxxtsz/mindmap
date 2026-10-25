"""独占加载文档的一次性校验凭据；不缓存公共可写文档的校验结果。"""

from .errors import MindMapError
from .models import MindMapDocument, MindMapPageView


class ValidatedDocument:
    """完整校验独占的新文档，在只读预检和引擎接管之间传递结果。

    构造时执行全部结构、字段及资源校验，失败不生成凭据。文档及其嵌套实体必须
    由加载器独占，取得凭据后只允许受信任的只读代码借用，不能修改或向扩展发布。
    它不是可写文档的永久校验缓存，也不代替宿主的布局、图片解码等界面预检。
    引擎成功接管时消费凭据，之后再次读取或使用会失败；接管被钩子拒绝时可重试。
    凭据不进入文件、历史或事件，可在线程间转交，但不能同时读写或重复接管。
    """

    __slots__ = ("_document",)

    def __init__(self, document: MindMapDocument):
        if not isinstance(document, MindMapDocument) or isinstance(document, MindMapPageView):
            raise TypeError("加载凭据必须持有完整文档")
        document.validate()
        self._document = document

    @property
    def document(self) -> MindMapDocument:
        """只读借用尚未接管的文档；调用方不得修改节点、集合或任何嵌套字段。"""
        if self._document is None:
            raise MindMapError("加载校验凭据已被消费")
        return self._document

    def require_document(self, document: MindMapDocument) -> None:
        """确认凭据仍有效且对应同一对象；不遍历、复制或再次校验文档。

        仅供遵守独占及只读约定的预检和接管入口调用。其他对象即使内容相等也拒绝，
        防止把一个文档的校验结果用于另一个候选；此查询不消费凭据。
        """
        if self.document is not document:
            raise MindMapError("加载校验凭据与候选文档不匹配")

    def consume(self, document: MindMapDocument) -> None:
        """在引擎完成提交前置检查后移交所有权；失败或取消前不得提前消费。"""
        self.require_document(document)
        self._document = None
