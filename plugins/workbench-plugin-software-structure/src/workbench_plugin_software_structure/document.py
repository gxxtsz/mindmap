"""校验软件结构文档，在原 JSON 文本中读写公共依赖阈值。"""

import json
import re
from codecs import BOM_UTF8
from dataclasses import dataclass
from pathlib import Path

MAX_COMPONENT_SIZE = 2_147_483_647
_THRESHOLDS_FIELD = "public_dependency_thresholds"
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
class PublicDependencyThresholds:
    """保存严格比较的阈值。模块数和直接依赖比例都要超过相应数值才归为公共依赖。"""

    component_size: int = 10
    direct_dependent_percent: int = 50

    def __post_init__(self) -> None:
        """对文件加载和侧栏输入使用同一组整数约束，错误包含完整字段名。"""
        for name, value, maximum in (
            ("component_size", self.component_size, MAX_COMPONENT_SIZE),
            ("direct_dependent_percent", self.direct_dependent_percent, 100),
        ):
            if type(value) is not int or not 0 <= value <= maximum:
                raise SoftwareStructureFormatError(f"{_THRESHOLDS_FIELD}.{name} 必须是 0 至 {maximum} 之间的整数")


@dataclass(frozen=True, slots=True)
class SoftwareStructure:
    """已校验的文档快照，包含模块目录、直接依赖和该文件的公共依赖阈值。"""

    modules: tuple[Module, ...]
    module_dependencies: tuple[ModuleDependency, ...] = ()
    public_dependency_thresholds: PublicDependencyThresholds = PublicDependencyThresholds()


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


def _reject_json_constant(value: str) -> None:
    raise SoftwareStructureFormatError(f"JSON 语法错误，数值 {value} 无效")


def load_software_structure(path: str | Path) -> SoftwareStructure:
    """从磁盘加载并校验版本 1 的 UTF-8 JSON 软件结构文档。"""
    return read_software_structure(path)[0]


def read_software_structure(path: str | Path) -> tuple[SoftwareStructure, bytes]:
    """同一次读取返回已校验内容和原始字节，供保存时定位字段和检查外部改动。

    支持 UTF-8 BOM、扩展字段、自依赖和循环依赖。读取或校验失败时抛出带中文说明的
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

    modules = _read_modules(data.get("modules"))
    dependencies = _read_dependencies(data.get("module_dependencies", []), {module.id for module in modules})
    settings = data.get(_THRESHOLDS_FIELD, {})
    if not isinstance(settings, dict):
        raise SoftwareStructureFormatError(f"{_THRESHOLDS_FIELD} 必须是对象")
    defaults = PublicDependencyThresholds()
    thresholds = PublicDependencyThresholds(
        settings.get("component_size", defaults.component_size),
        settings.get("direct_dependent_percent", defaults.direct_dependent_percent),
    )
    return SoftwareStructure(modules, dependencies, thresholds), snapshot


def _json_object_members(content: str, start: int) -> tuple[dict[str, tuple[int, int]], int, str]:
    """记录已校验对象的值范围、追加位置和首个字段的缩进。"""
    decode = json.JSONDecoder().raw_decode
    position = _JSON_SPACE.match(content, start).end() + 1
    end = position
    members = {}
    spacing = " "
    while content[key_start := _JSON_SPACE.match(content, position).end()] != "}":
        if not members:
            spacing = content[position:key_start] or spacing
        key, after_key = decode(content, key_start)
        colon = _JSON_SPACE.match(content, after_key).end()
        value_start = _JSON_SPACE.match(content, colon + 1).end()
        # 用 JSON 解码器跨过整个值。字符串转义、嵌套对象和数组中的分隔符都按 JSON 语义处理。
        _, end = decode(content, value_start)
        members[key] = (value_start, end)
        delimiter = _JSON_SPACE.match(content, end).end()
        if content[delimiter] == "}":
            break
        position = delimiter + 1
    return members, end, spacing


def rewrite_public_dependency_thresholds(snapshot: bytes, thresholds: PublicDependencyThresholds) -> bytes:
    """在读取器返回的原始快照上编辑两个阈值，保留其余 JSON 文本、换行与 BOM。"""
    content = snapshot.decode("utf-8-sig")
    members, tail, spacing = _json_object_members(content, 0)
    values = {"component_size": thresholds.component_size,
              "direct_dependent_percent": thresholds.direct_dependent_percent}
    if bounds := members.get(_THRESHOLDS_FIELD):
        members, tail, spacing = _json_object_members(content, bounds[0])
        fields = {key: str(value) for key, value in values.items()}
    else:
        fields = {_THRESHOLDS_FIELD: json.dumps(values)}

    edits = []
    new_members = []
    for key, value in fields.items():
        if key in members:
            start, end = members[key]
            edits.append((start, end, value))
        else:
            new_members.append(f'{json.dumps(key)}: {value}')
    if new_members:
        separator = "," + spacing
        addition = (separator if members else "") + separator.join(new_members)
        edits.append((tail, tail, addition))
    # 从后往前应用修改，前面的字段范围可继续使用原文坐标。
    for start, end, value in sorted(edits, reverse=True):
        content = content[:start] + value + content[end:]
    bom = BOM_UTF8 if snapshot.startswith(BOM_UTF8) else b""
    return bom + content.encode("utf-8")
