"""读取软件结构文档并校验共享模块目录和模块依赖。"""

import json
from dataclasses import dataclass
from pathlib import Path


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
class SoftwareStructure:
    """已校验的只读文档内容，供同一文件的多个标签共享。"""

    modules: tuple[Module, ...]
    module_dependencies: tuple[ModuleDependency, ...] = ()


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
    """从磁盘加载版本 1 的 UTF-8 JSON 软件结构文档。

    支持 UTF-8 BOM 和额外字段。已识别的字段须符合类型和引用约束，
    自依赖及循环依赖均是有效数据。任何读取或校验失败都抛出带中文说明的
    SoftwareStructureFormatError，错误消息包含可用的行列或字段位置。
    """
    try:
        content = Path(path).read_text(encoding="utf-8-sig")
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
    return SoftwareStructure(modules, dependencies)
