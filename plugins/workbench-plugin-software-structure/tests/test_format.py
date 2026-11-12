"""核对软件结构文档的版本、编码、字段和引用契约。"""

import json
from importlib.resources import as_file, files

import pytest
from workbench_plugin_software_structure.document import (
    MAX_COMPONENT_SIZE,
    Module,
    ModuleDependency,
    PublicDependencyThresholds,
    SoftwareStructure,
    SoftwareStructureFormatError,
    load_software_structure,
    read_software_structure,
    rewrite_public_dependency_thresholds,
)


def test_reads_bom_case_sensitive_ids_and_extra_fields(tmp_path):
    path = tmp_path / "系统.software-structure"
    data = {
        "schema_version": 1,
        "modules": [
            {"id": "Core", "name": "核心", "description": "共享业务能力", "extensions": {}},
            {"id": "core", "name": "适配"},
        ],
        "module_dependencies": [
            {"source": "core", "target": "Core"},
            {"source": "Core", "target": "core"},
            {"source": "Core", "target": "Core"},
        ],
        "apis": [{"module_id": "Core"}],
        "architecture": {"views": []},
    }
    path.write_text(json.dumps(data, ensure_ascii=False), "utf-8-sig")
    assert load_software_structure(path) == SoftwareStructure(
        (Module("Core", "核心", "共享业务能力"), Module("core", "适配")),
        (ModuleDependency("core", "Core"), ModuleDependency("Core", "core"), ModuleDependency("Core", "Core")),
    )


def test_empty_graph_and_omitted_dependencies(tmp_path):
    path = tmp_path / "空图.software-structure"
    path.write_text('{"schema_version":1,"modules":[]}', "utf-8")
    assert load_software_structure(path) == SoftwareStructure(())


@pytest.mark.parametrize(("fields", "expected"), [
    ({}, PublicDependencyThresholds()),
    ({"component_size": 0}, PublicDependencyThresholds(0, 50)),
    ({"direct_dependent_percent": 100}, PublicDependencyThresholds(10, 100)),
    ({"component_size": MAX_COMPONENT_SIZE, "direct_dependent_percent": 0},
     PublicDependencyThresholds(MAX_COMPONENT_SIZE, 0)),
])
def test_thresholds_default_each_omitted_field(tmp_path, fields, expected):
    """两个可选阈值可分别缺省，允许输入控件支持的端点。"""
    path = tmp_path / "阈值.software-structure"
    path.write_text(json.dumps({"schema_version": 1, "modules": [], "public_dependency_thresholds": fields}),
                    encoding="utf-8")
    assert load_software_structure(path).public_dependency_thresholds == expected


def test_threshold_rewrite_preserves_bom_and_unrelated_json_text(tmp_path):
    """保留 CRLF、制表符和扩展值的数字字面量，同时覆盖旧值并补入缺失项。"""
    path = tmp_path / "保留原文.software-structure"
    original = (
        '\ufeff{\r\n'
        '\t"schema_version": 1,\r\n'
        '\t"modules": [{"id": "core", "name": "核心 } \\"模块", "component_size": 4}],\r\n'
        '\t"public_dependency_thresholds": {\r\n'
        '\t\t"component_size": 4,\r\n'
        '\t\t"扩展": {"direct_dependent_percent": 1.234567890123456789e400}\r\n'
        '\t},\r\n'
        '\t"其它": ["public_dependency_thresholds", "}"]\r\n'
        '}\r\n'
    ).encode("utf-8")
    path.write_bytes(original)
    document, snapshot = read_software_structure(path)
    assert document.public_dependency_thresholds == PublicDependencyThresholds(4, 50)
    assert snapshot == original
    thresholds = PublicDependencyThresholds(0, 100)
    updated = rewrite_public_dependency_thresholds(snapshot, thresholds)
    assert updated == original.replace(b'\t\t"component_size": 4,', b'\t\t"component_size": 0,').replace(
        b'e400}\r\n\t},', b'e400},\r\n\t\t"direct_dependent_percent": 100\r\n\t},',
    )
    path.write_bytes(updated)
    assert load_software_structure(path).public_dependency_thresholds == thresholds


@pytest.mark.parametrize("members", [b"", b',"public_dependency_thresholds":{}',
                                     b',"public_dependency_thresholds":{"component_size":1}'])
def test_threshold_rewrite_adds_missing_fields(tmp_path, members):
    """缺少设置对象、空对象或缺少单项时均可写入两个值。"""
    path = tmp_path / "缺省阈值.software-structure"
    snapshot = b'{"schema_version":1,"modules":[]' + members + b'}\n'
    thresholds = PublicDependencyThresholds(10, 60)
    path.write_bytes(rewrite_public_dependency_thresholds(snapshot, thresholds))
    assert load_software_structure(path) == SoftwareStructure((), public_dependency_thresholds=thresholds)


def test_bundled_template_is_a_valid_graph():
    """从已安装的插件资源取得下载模板，使用正式读取器验证示例图数据。"""
    resource = files("workbench_plugin_software_structure").joinpath("resources", "软件结构模板.software-structure")
    with as_file(resource) as path:
        template = load_software_structure(path)
    assert template.modules and template.module_dependencies


@pytest.mark.parametrize(("overrides", "message"), [
    ({"schema_version": True}, "schema_version 必须是整数"),
    ({"schema_version": 1.0}, "schema_version 必须是整数"),
    ({"schema_version": 2}, "暂不支持 schema_version"),
    ({"modules": None}, "modules 必须是数组"),
    ({"modules": [1]}, "modules[0] 必须是对象"),
    ({"modules": [{"id": " ", "name": "应用"}]}, "modules[0].id 必须是非空字符串"),
    ({"modules": [{"id": "app", "name": 12}]}, "modules[0].name 必须是非空字符串"),
    ({"modules": [{"id": "app", "name": "应用", "description": None}]}, "modules[0].description 必须是字符串"),
    ({"modules": [{"id": "app", "name": "应用"}] * 2}, "modules[1].id 与前面的模块 ID 重复"),
    ({"module_dependencies": {}}, "module_dependencies 必须是数组"),
    ({"module_dependencies": [False]}, "module_dependencies[0] 必须是对象"),
    ({"module_dependencies": [{"target": "app"}]}, "module_dependencies[0].source 必须是非空字符串"),
    ({"module_dependencies": [{"source": "app", "target": "App"}]}, "module_dependencies[0].target 引用了未声明"),
    ({"module_dependencies": [{"source": "app", "target": "app"}] * 2}, "module_dependencies[1] 与前面的依赖重复"),
    ({"public_dependency_thresholds": None}, "public_dependency_thresholds 必须是对象"),
    ({"public_dependency_thresholds": {"component_size": True}}, "public_dependency_thresholds.component_size"),
    ({"public_dependency_thresholds": {"component_size": -1}}, "public_dependency_thresholds.component_size"),
    ({"public_dependency_thresholds": {"component_size": MAX_COMPONENT_SIZE + 1}},
     "public_dependency_thresholds.component_size"),
    ({"public_dependency_thresholds": {"direct_dependent_percent": 50.0}},
     "public_dependency_thresholds.direct_dependent_percent"),
    ({"public_dependency_thresholds": {"direct_dependent_percent": 101}},
     "public_dependency_thresholds.direct_dependent_percent"),
])
def test_invalid_fields_include_their_location(tmp_path, overrides, message):
    path = tmp_path / "损坏.software-structure"
    data = {"schema_version": 1, "modules": [{"id": "app", "name": "应用"}], **overrides}
    path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    with pytest.raises(SoftwareStructureFormatError) as raised:
        load_software_structure(path)
    assert message in str(raised.value)


@pytest.mark.parametrize(("content", "message"), [
    (b"\xff", "必须使用 UTF-8"),
    (b'{\n  "schema_version": 1,\n}', "第 3 行第 1 列"),
    (b'{"schema_version":1,"modules":[],"extra":NaN}', "JSON 语法错误"),
    (b"[]", "顶层必须是对象"),
    (b'{"schema_version":1}', "modules 必须是数组"),
])
def test_rejects_invalid_documents(tmp_path, content, message):
    path = tmp_path / "损坏.software-structure"
    path.write_bytes(content)
    with pytest.raises(SoftwareStructureFormatError, match=message):
        load_software_structure(path)
