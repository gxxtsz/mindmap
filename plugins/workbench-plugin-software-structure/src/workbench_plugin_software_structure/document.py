"""校验软件结构文档，并在原 JSON 文本中保存连线筛选与格式说明。"""

import json
import re
from codecs import BOM_UTF8
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

SOFTWARE_STRUCTURE_SUFFIX = "software-structure"
TEMPLATE_FILENAME = f"软件结构模板.{SOFTWARE_STRUCTURE_SUFFIX}"

_FORMAT_DESCRIPTION = "format_description"
_HIDDEN_EDGE_MODULE_IDS = "hidden_edge_module_ids"
_LEGACY_PUBLIC_DEPENDENCY = "is_public_dependency"
_JSON_SPACE = re.compile(r"[ \t\r\n]*")


class SoftwareStructureFormatError(ValueError):
    """文件读取或格式校验失败，消息可直接在状态栏和日志中展示。"""


@dataclass(frozen=True, slots=True)
class Module:
    """共享模块目录中的条目。标识区分大小写，名称和说明用于界面展示。"""

    id: str
    name: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class ModuleDependency:
    """一条有向依赖。source 是依赖方，target 是被依赖方，均为模块标识。"""

    source: str
    target: str


@dataclass(frozen=True, slots=True)
class ModuleApi:
    """供读者浏览的接口名称和用途。类的成员沿用相同结构，按文件顺序保存。"""

    name: str
    description: str
    kind: str | None = None
    members: tuple["ModuleApi", ...] = ()


@dataclass(frozen=True, slots=True)
class ModuleApiGroup:
    """归属于一个已声明模块的接口清单，空元组表示暂无符合收录范围的接口。"""

    module_id: str
    apis: tuple[ModuleApi, ...]


@dataclass(frozen=True, slots=True)
class SoftwareStructure:
    """已校验的文档快照，模块和依赖按文件顺序排列。

    hidden_edge_module_ids 记录勾选的模块。依赖任意一端在集合中时，筛选视图
    隐藏该依赖的线条和箭头。集合中的标识均来自当前文档的模块目录。
    module_apis 只包含文件显式提供的模块条目，侧栏据此区分缺少说明和空清单。
    """

    modules: tuple[Module, ...]
    module_dependencies: tuple[ModuleDependency, ...] = ()
    hidden_edge_module_ids: frozenset[str] = frozenset()
    module_apis: tuple[ModuleApiGroup, ...] = ()


def _required_text(record: dict, key: str, location: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SoftwareStructureFormatError(f"{location}.{key} 必须是非空字符串")
    return value


def _read_modules(records: object) -> tuple[Module, ...]:
    if not isinstance(records, list):
        raise SoftwareStructureFormatError("modules 必须是数组")
    modules = []
    module_ids = set()
    for index, record in enumerate(records):
        location = f"modules[{index}]"
        if not isinstance(record, dict):
            raise SoftwareStructureFormatError(f"{location} 必须是对象")
        module_id = _required_text(record, "id", location)
        name = _required_text(record, "name", location)
        description = record.get("description", "")
        if not isinstance(description, str):
            raise SoftwareStructureFormatError(f"{location}.description 必须是字符串")
        if module_id in module_ids:
            raise SoftwareStructureFormatError(f"{location}.id 与前面的模块 ID 重复（{module_id!r}）")
        module_ids.add(module_id)
        modules.append(Module(module_id, name, description))
    return tuple(modules)


def _read_dependencies(records: object, module_ids: set[str]) -> tuple[ModuleDependency, ...]:
    if not isinstance(records, list):
        raise SoftwareStructureFormatError("module_dependencies 必须是数组")
    dependencies = []
    seen = set()
    for index, record in enumerate(records):
        location = f"module_dependencies[{index}]"
        if not isinstance(record, dict):
            raise SoftwareStructureFormatError(f"{location} 必须是对象")
        source = _required_text(record, "source", location)
        target = _required_text(record, "target", location)
        for field, module_id in (("source", source), ("target", target)):
            if module_id not in module_ids:
                raise SoftwareStructureFormatError(f"{location}.{field} 引用了未声明的模块 ID（{module_id!r}）")
        dependency = ModuleDependency(source, target)
        if dependency in seen:
            raise SoftwareStructureFormatError(f"{location} 与前面的依赖重复（{source!r} → {target!r}）")
        seen.add(dependency)
        dependencies.append(dependency)
    return tuple(dependencies)


def _read_api_entries(records: object, location: str) -> tuple[ModuleApi, ...]:
    """按原顺序读取接口或类成员，递归校验时将数组下标保留在中文错误位置中。"""
    if not isinstance(records, list):
        raise SoftwareStructureFormatError(f"{location} 必须是数组")
    entries = []
    for index, record in enumerate(records):
        entry_location = f"{location}[{index}]"
        if not isinstance(record, dict):
            raise SoftwareStructureFormatError(f"{entry_location} 必须是对象")
        name = _required_text(record, "name", entry_location)
        description = _required_text(record, "description", entry_location)
        kind = record.get("kind")
        if "kind" in record and kind != "class":
            raise SoftwareStructureFormatError(f'{entry_location}.kind 必须是字符串 "class"')
        members = ()
        if "members" in record:
            if kind != "class":
                raise SoftwareStructureFormatError(f"{entry_location}.members 只能在类条目中填写")
            members = _read_api_entries(record["members"], f"{entry_location}.members")
        entries.append(ModuleApi(name, description, kind, members))
    return tuple(entries)


def _read_module_apis(records: object, module_ids: set[str]) -> tuple[ModuleApiGroup, ...]:
    """读取可选的接口分组，并按原文核对模块 ID 及其唯一性。

    调用方将缺省字段传为空数组。分组内的 apis 必填，显式空数组也保留为一个分组，
    让侧栏能将其与尚未提供信息的模块区分开。
    """
    if not isinstance(records, list):
        raise SoftwareStructureFormatError("module_apis 必须是数组")
    groups = []
    seen = set()
    for index, record in enumerate(records):
        location = f"module_apis[{index}]"
        if not isinstance(record, dict):
            raise SoftwareStructureFormatError(f"{location} 必须是对象")
        module_id = _required_text(record, "module_id", location)
        if module_id not in module_ids:
            raise SoftwareStructureFormatError(f"{location}.module_id 引用了未声明的模块 ID（{module_id!r}）")
        if module_id in seen:
            raise SoftwareStructureFormatError(f"{location}.module_id 与前面的模块 ID 重复（{module_id!r}）")
        seen.add(module_id)
        groups.append(ModuleApiGroup(module_id, _read_api_entries(record.get("apis"), f"{location}.apis")))
    return tuple(groups)


def _read_hidden_edge_module_ids(records: object, module_ids: set[str]) -> frozenset[str]:
    """校验筛选列表中的每个条目，报告无效或重复标识所在的数组下标。"""
    if not isinstance(records, list):
        raise SoftwareStructureFormatError(f"{_HIDDEN_EDGE_MODULE_IDS} 必须是数组")
    hidden = set()
    for index, module_id in enumerate(records):
        location = f"{_HIDDEN_EDGE_MODULE_IDS}[{index}]"
        if not isinstance(module_id, str) or not module_id.strip():
            raise SoftwareStructureFormatError(f"{location} 必须是非空字符串")
        if module_id not in module_ids:
            raise SoftwareStructureFormatError(f"{location} 引用了未声明的模块 ID（{module_id!r}）")
        if module_id in hidden:
            raise SoftwareStructureFormatError(f"{location} 与前面的模块 ID 重复（{module_id!r}）")
        hidden.add(module_id)
    return frozenset(hidden)


def _read_legacy_edge_filter(records: list[dict], modules: tuple[Module, ...]) -> frozenset[str]:
    """新字段缺省时读取旧勾选，按已校验的模块顺序迁移为隐藏连线的标识集合。"""
    hidden = set()
    for index, (record, module) in enumerate(zip(records, modules, strict=True)):
        marked = record.get(_LEGACY_PUBLIC_DEPENDENCY, False)
        if type(marked) is not bool:
            raise SoftwareStructureFormatError(f"modules[{index}].{_LEGACY_PUBLIC_DEPENDENCY} 必须是布尔值")
        if marked:
            hidden.add(module.id)
    return frozenset(hidden)


def _reject_json_constant(value: str) -> None:
    raise SoftwareStructureFormatError(f"JSON 语法错误，数值 {value} 无效")


def read_bundled_template() -> bytes:
    """读取随插件分发的版本 1 模板原文，供下载和旧文件补写格式说明共用。"""
    return files(__package__).joinpath("resources", TEMPLATE_FILENAME).read_bytes()


def load_software_structure(path: str | Path) -> SoftwareStructure:
    """从磁盘加载并校验版本 1 的 UTF-8 JSON 软件结构文档。"""
    return read_software_structure(path)[0]


def read_software_structure(path: str | Path) -> tuple[SoftwareStructure, bytes]:
    """同一次读取返回已校验内容和原始字节，供保存时定位字段和检查外部改动。

    支持 UTF-8 BOM、扩展字段、自依赖和循环依赖。接口清单是版本 1 的可选字段，
    通过模块 ID 与模块目录关联。兼容缺少格式说明的旧文件，保存筛选时再补齐。
    优先读取顶层筛选列表（包括空列表），缺省时在内存中迁移旧的公共依赖勾选。
    读取或校验失败时抛出带中文说明的
    SoftwareStructureFormatError，错误消息包含可用的行列或字段位置。
    """
    try:
        snapshot = Path(path).read_bytes()
        content = snapshot.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SoftwareStructureFormatError("软件结构文件必须使用 UTF-8 编码") from error
    except OSError as error:
        raise SoftwareStructureFormatError(f"无法读取软件结构文件 {path}（{error.strerror or error}）") from error

    try:
        data = json.loads(content, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as error:
        raise SoftwareStructureFormatError(f"JSON 语法错误，位于第 {error.lineno} 行第 {error.colno} 列") from error
    except SoftwareStructureFormatError:
        raise
    except (ValueError, RecursionError) as error:
        raise SoftwareStructureFormatError("无法解析 JSON，请检查数值范围或对象嵌套深度") from error

    if not isinstance(data, dict):
        raise SoftwareStructureFormatError("软件结构文件的顶层必须是对象")
    version = data.get("schema_version")
    if type(version) is not int:
        raise SoftwareStructureFormatError("schema_version 必须是整数")
    if version != 1:
        raise SoftwareStructureFormatError(f"暂不支持 schema_version 为 {version} 的软件结构文件")

    records = data.get("modules")
    modules = _read_modules(records)
    module_ids = {module.id for module in modules}
    dependencies = _read_dependencies(data.get("module_dependencies", []), module_ids)
    hidden = (_read_hidden_edge_module_ids(data[_HIDDEN_EDGE_MODULE_IDS], module_ids)
              if _HIDDEN_EDGE_MODULE_IDS in data else _read_legacy_edge_filter(records, modules))
    module_apis = _read_module_apis(data.get("module_apis", []), module_ids)
    return SoftwareStructure(modules, dependencies, hidden, module_apis), snapshot


@dataclass(frozen=True, slots=True)
class _JsonMember:
    """已校验对象的直接成员。start 指向成员前的空白，end 紧跟字段值。"""

    key: str
    start: int
    value_start: int
    end: int


def _json_object_members(content: str, start: int) -> tuple[list[_JsonMember], str]:
    """按原文顺序定位对象的直接成员与首个成员的缩进，保留重复键的位置。"""
    decode = json.JSONDecoder().raw_decode
    position = _JSON_SPACE.match(content, start).end() + 1
    members = []
    spacing = " "
    while content[key_start := _JSON_SPACE.match(content, position).end()] != "}":
        if not members:
            spacing = content[position:key_start] or spacing
        key, after_key = decode(content, key_start)
        colon = _JSON_SPACE.match(content, after_key).end()
        value_start = _JSON_SPACE.match(content, colon + 1).end()
        # 用 JSON 解码器跨过整个值。字符串转义、嵌套对象和数组中的分隔符都按 JSON 语义处理。
        _, end = decode(content, value_start)
        members.append(_JsonMember(key, position, value_start, end))
        delimiter = _JSON_SPACE.match(content, end).end()
        if content[delimiter] == "}":
            break
        position = delimiter + 1
    return members, spacing


def _legacy_mark_ranges(content: str, array_start: int, count: int) -> Iterator[tuple[int, int, str]]:
    """定位旧标记及其分隔逗号。连续的旧键一起删除，嵌套扩展字段留在原文中。"""
    position = array_start + 1
    for _ in range(count):
        members, _ = _json_object_members(content, position)
        previous_end = position
        removal_start = None
        for member in members:
            if member.key == _LEGACY_PUBLIC_DEPENDENCY:
                if removal_start is None:
                    removal_start = member.start
            else:
                if removal_start is not None:
                    yield removal_start, member.start, ""
                    removal_start = None
                previous_end = member.end
        tail = members[-1].end
        if removal_start is not None:
            yield previous_end, tail, ""
        # 跨过模块对象的右花括号和数组分隔逗号，下一轮从下一个对象前的空白开始。
        after_object = _JSON_SPACE.match(content, tail).end() + 1
        position = _JSON_SPACE.match(content, after_object).end() + 1


def rewrite_edge_filter(snapshot: bytes, document: SoftwareStructure) -> bytes:
    """生成包含筛选设置和格式说明的候选文件，供调用方一次原子提交。

    document 来自已校验的 snapshot，仅更新了 hidden_edge_module_ids。按模块顺序
    写入标识数组，并移除模块对象上的旧公共依赖标记。缺少格式说明时从模板补写。
    已有说明、其余字段、换行与 UTF-8 BOM 均取自原文。
    """
    content = snapshot.decode("utf-8-sig")
    members, spacing = _json_object_members(content, 0)
    fields = {member.key: member for member in members}
    edits = list(_legacy_mark_ranges(content, fields["modules"].value_start, len(document.modules)))

    if _FORMAT_DESCRIPTION not in fields:
        description = json.loads(read_bundled_template())[_FORMAT_DESCRIPTION]
        # 在版本号后插入说明，沿用顶层缩进和换行，紧凑的单行文件也按原格式写入。
        last_newline = max(spacing.rfind("\n"), spacing.rfind("\r"))
        indent = spacing[last_newline + 1:] if last_newline >= 0 else None
        description_text = json.dumps(description, ensure_ascii=False, indent=indent).replace("\n", spacing)
        version_end = fields["schema_version"].end
        edits.append((version_end, version_end, f',{spacing}"{_FORMAT_DESCRIPTION}": {description_text}'))

    value = json.dumps([module.id for module in document.modules if module.id in document.hidden_edge_module_ids])
    if field := fields.get(_HIDDEN_EDGE_MODULE_IDS):
        edits.append((field.value_start, field.end, value))
    else:
        tail = members[-1].end
        edits.append((tail, tail, f',{spacing}"{_HIDDEN_EDGE_MODULE_IDS}": {value}'))

    parts = []
    position = 0
    # 版本号位于末尾时，两项补写可能共用同一位置。按原添加顺序写入这两项。
    for start, end, replacement in sorted(edits, key=lambda edit: edit[0]):
        parts.extend((content[position:start], replacement))
        position = end
    parts.append(content[position:])
    bom = BOM_UTF8 if snapshot.startswith(BOM_UTF8) else b""
    return bom + "".join(parts).encode("utf-8")
