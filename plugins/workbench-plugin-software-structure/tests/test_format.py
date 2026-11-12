"""核对软件结构文档的版本、编码、字段和引用契约。"""

import json
from dataclasses import replace
from importlib.resources import as_file, files

import pytest
from workbench_plugin_software_structure.document import (
    Module,
    ModuleDependency,
    SoftwareStructure,
    SoftwareStructureFormatError,
    load_software_structure,
    read_software_structure,
    rewrite_edge_filter,
)


def test_reads_bom_case_sensitive_ids_and_extra_fields(tmp_path):
    path = tmp_path / "系统.software-structure"
    data = {
        "schema_version": 1,
        "modules": [
            {"id": "Core", "name": "核心", "description": "共享业务能力",
             "is_public_dependency": True, "extensions": {}},
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
        frozenset({"Core"}),
    )


def test_empty_graph_and_omitted_dependencies(tmp_path):
    path = tmp_path / "空图.software-structure"
    path.write_text('{"schema_version":1,"modules":[]}', "utf-8")
    assert load_software_structure(path) == SoftwareStructure(())


@pytest.mark.parametrize("hidden", [[], ["core"]])
def test_saved_filter_takes_precedence_over_legacy_marks(tmp_path, hidden):
    """新字段存在时以其为准，空列表也能覆盖旧勾选。"""
    path = tmp_path / "筛选优先.software-structure"
    path.write_text(json.dumps({
        "schema_version": 1,
        "modules": [{"id": "Core", "name": "核心", "is_public_dependency": True}, {"id": "core", "name": "适配"}],
        "hidden_edge_module_ids": hidden,
    }), "utf-8")
    assert load_software_structure(path).hidden_edge_module_ids == frozenset(hidden)


def test_filter_rewrite_migrates_legacy_keys_and_preserves_original_text(tmp_path):
    """在一次重写中移除直接旧键，保留 CRLF、缩进、旧阈值和扩展字段。"""
    path = tmp_path / "保留原文.software-structure"
    original = (
        '\ufeff{\r\n'
        '\t"schema_version": 1,\r\n'
        '\t"modules": [\r\n'
        '\t\t{"is_public_dependency": false, "id": "core", "name": "核心 } \\"模块", '
        '"is_public_dependency": false},\r\n'
        '\t\t{\r\n'
        '\t\t\t"id": "Core",\r\n'
        '\t\t\t"is_public_dependency": false,\r\n'
        '\t\t\t"is_public_\\u0064ependency": true,\r\n'
        '\t\t\t"name": "核心 } \\"模块",\r\n'
        '\t\t\t"扩展": {"is_public_dependency": 1.234567890123456789e400}\r\n'
        '\t\t}\r\n'
        '\t],\r\n'
        '\t"public_dependency_thresholds": {"component_size": 0, "direct_dependent_percent": 0},\r\n'
        '\t"其它": ["is_public_dependency", "}"]\r\n'
        '}\r\n'
    ).encode("utf-8")
    path.write_bytes(original)
    document, snapshot = read_software_structure(path)
    assert document.hidden_edge_module_ids == {"Core"}
    assert snapshot == original == path.read_bytes()
    checked = replace(document, hidden_edge_module_ids=frozenset({"Core", "core"}))
    updated = rewrite_edge_filter(snapshot, checked)
    expected = (
        original.replace(b'"is_public_dependency": false,', b'', 1)
        .replace(b', "is_public_dependency": false}', b'}', 1)
        .replace(b'\t\t\t"is_public_dependency": false,\r\n\t\t\t"is_public_\\u0064ependency": true,\r\n', b'')
        .replace(b'\r\n}\r\n', b',\r\n\t"hidden_edge_module_ids": ["core", "Core"]\r\n}\r\n')
    )
    assert updated == expected
    path.write_bytes(updated)
    assert load_software_structure(path) == checked
    cleared = replace(document, hidden_edge_module_ids=frozenset())
    unchecked = rewrite_edge_filter(updated, cleared)
    assert unchecked == updated.replace(b'["core", "Core"]', b'[]')
    path.write_bytes(unchecked)
    assert load_software_structure(path) == cleared


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
    ({"modules": [{"id": "app", "name": "应用", "is_public_dependency": None}]},
     "modules[0].is_public_dependency 必须是布尔值"),
    ({"modules": [{"id": "app", "name": "应用", "is_public_dependency": 1}]},
     "modules[0].is_public_dependency 必须是布尔值"),
    ({"modules": [{"id": "app", "name": "应用", "is_public_dependency": "true"}]},
     "modules[0].is_public_dependency 必须是布尔值"),
    ({"modules": [{"id": "app", "name": "应用"}] * 2}, "modules[1].id 与前面的模块 ID 重复"),
    ({"module_dependencies": {}}, "module_dependencies 必须是数组"),
    ({"module_dependencies": [False]}, "module_dependencies[0] 必须是对象"),
    ({"module_dependencies": [{"target": "app"}]}, "module_dependencies[0].source 必须是非空字符串"),
    ({"module_dependencies": [{"source": "app", "target": "App"}]}, "module_dependencies[0].target 引用了未声明"),
    ({"module_dependencies": [{"source": "app", "target": "app"}] * 2}, "module_dependencies[1] 与前面的依赖重复"),
    ({"hidden_edge_module_ids": None}, "hidden_edge_module_ids 必须是数组"),
    ({"hidden_edge_module_ids": [True]}, "hidden_edge_module_ids[0] 必须是非空字符串"),
    ({"hidden_edge_module_ids": ["App"]}, "hidden_edge_module_ids[0] 引用了未声明"),
    ({"hidden_edge_module_ids": ["app", "app"]}, "hidden_edge_module_ids[1] 与前面的模块 ID 重复"),
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
