"""加载校验凭据的独占转交、一次性使用及替换失败保护。"""

from copy import deepcopy

import pytest
from mindmap_core import MindMapDocument, MindMapEngine, MindMapError, OperationRejected, ValidatedDocument


def test_validated_adoption_checks_once_and_resets_history(monkeypatch):
    engine = MindMapEngine()
    engine.add_child(engine.root_id, "原文档未保存修改")
    document = MindMapDocument.new("独占读取结果")
    calls, events = [], []
    validate = MindMapDocument.validate

    def counted(candidate):
        calls.append(candidate)
        return validate(candidate)

    monkeypatch.setattr(MindMapDocument, "validate", counted)
    validation = ValidatedDocument(document)
    engine.subscribe(events.append)
    engine.adopt_document(document, validation=validation)
    assert calls == [document]
    assert engine.document == document and not engine.is_dirty and not engine.can_undo
    assert engine.document_generation == 1 and events[-1].kind == "reset"
    with pytest.raises(MindMapError, match="已被消费"):
        engine.adopt_document(document, validation=validation)
    assert engine.document_generation == 1


def test_validation_cannot_be_used_for_equal_but_distinct_document():
    engine = MindMapEngine()
    document = MindMapDocument.new("同内容不同对象")
    validation = ValidatedDocument(document)
    before, revision = engine.document, engine.revision
    with pytest.raises(MindMapError, match="不匹配"):
        engine.adopt_document(deepcopy(document), validation=validation)
    assert engine.document == before and engine.revision == revision
    assert validation.document is document


def test_rejected_adoption_does_not_consume_validation_or_change_engine():
    engine = MindMapEngine()
    document = MindMapDocument.new("等待接管")
    validation = ValidatedDocument(document)
    before = engine.document
    engine.before_operation = lambda *args: False
    with pytest.raises(OperationRejected):
        engine.adopt_document(document, validation=validation)
    assert engine.document == before and validation.document is document
    engine.before_operation = None
    with pytest.raises(MindMapError, match="事务"):
        with engine.transaction():
            with pytest.raises(MindMapError, match="事务"):
                engine.adopt_document(document, validation=validation)
    assert engine.document == before and validation.document is document
    engine.adopt_document(document, validation=validation)
    assert engine.document == document


def test_invalid_input_cannot_produce_validation():
    document = MindMapDocument.new()
    document.root.children.append("缺失的节点")
    with pytest.raises(MindMapError, match="不存在"):
        ValidatedDocument(document)
    with pytest.raises(TypeError, match="完整文档"):
        ValidatedDocument(MindMapDocument.new().page_view())
