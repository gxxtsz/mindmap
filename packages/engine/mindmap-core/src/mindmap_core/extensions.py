"""扩展协议和实例级注册表；第三方扩展只需依赖本核心包。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from math import isfinite
from pathlib import PurePath
from typing import Any, BinaryIO, Protocol

from .config import Direction, LayoutConfig
from .errors import MindMapError
from .geometry import BranchPath, Curve, LayoutResult, PathCommand, Point
from .models import MindMapDocument


@dataclass(frozen=True)
class LayoutInfo:
    """布局菜单所需的声明；默认配置使用独立副本，方向与紧凑能力由界面读取。"""

    id: str
    name: str
    default_config: LayoutConfig = field(default_factory=LayoutConfig)
    directions: tuple[Direction, ...] = ()
    supports_compact: bool = False
    supports_branch_styles: bool = False


class LayoutAlgorithm(Protocol):
    """纯布局算法协议；实现不得修改文档或测量输入，不负责实际文字测量。"""

    info: LayoutInfo

    def validate_config(self, config: LayoutConfig) -> None:
        """验证当前算法支持的方向、紧凑能力及专有选项，失败抛出中文异常。"""
        ...

    def layout(
        self,
        document: MindMapDocument,
        measurements: Mapping[str, Point] | None = None,
        *,
        expand_all: bool = False,
        focus_id: str | None = None,
    ) -> LayoutResult:
        """返回可见节点、父子曲线和完整内容边界，聚焦与展开仅影响本次计算。"""
        ...


class LayoutRegistry:
    """按稳定标识注册和调度布局；实例之间没有隐式共享或自动发现行为。"""

    def __init__(self):
        self._algorithms: dict[str, LayoutAlgorithm] = {}
        self._info: dict[str, LayoutInfo] = {}
        self._copy_inputs: dict[str, bool] = {}

    def register(self, algorithm: LayoutAlgorithm, *, copy_inputs: bool = True) -> None:
        """注册布局，校验元信息及默认配置；重复标识明确拒绝。

        copy_inputs 默认开启，以独立副本隔离扩展对文档和测量表的修改。仅当宿主
        确认实现完全只读时可显式关闭，例如本包组合入口注册的内置树布局；关闭后
        实现不得保留可变输入或改写它们。输入和输出的公共契约校验始终执行。
        """
        if not isinstance(copy_inputs, bool):
            raise MindMapError("布局输入隔离选项必须为布尔值")
        try:
            info = deepcopy(algorithm.info)
            if not isinstance(info, LayoutInfo) or not isinstance(info.default_config, LayoutConfig):
                raise MindMapError("布局必须提供 LayoutInfo 与默认 LayoutConfig")
            _validate_identity(info.id, info.name)
            if info.id in self._algorithms:
                raise MindMapError(f"布局算法标识重复：{info.id}")
            info.default_config.validate()
            if info.default_config.algorithm_id != info.id:
                raise MindMapError("默认布局配置与算法标识不一致")
            if any(direction not in ("left", "right", "both", "down") for direction in info.directions):
                raise MindMapError("布局声明包含无效方向")
            if not isinstance(info.supports_compact, bool):
                raise MindMapError("布局紧凑能力必须为布尔值")
            if not isinstance(info.supports_branch_styles, bool):
                raise MindMapError("布局分支样式能力必须为布尔值")
            if info.directions and info.default_config.direction not in info.directions:
                raise MindMapError("默认布局方向不在算法支持范围内")
            if info.default_config.compact and not info.supports_compact:
                raise MindMapError("默认布局配置启用了不支持的紧凑模式")
            algorithm.validate_config(deepcopy(info.default_config))
        except MindMapError:
            raise
        except Exception as exc:
            raise MindMapError(f"注册布局算法失败：{exc}") from exc
        self._algorithms[info.id] = algorithm
        self._info[info.id] = info
        self._copy_inputs[info.id] = copy_inputs

    def get(self, algorithm_id: str) -> LayoutAlgorithm:
        """查找已注册实现；缺失时不回退到其他算法。"""
        try:
            return self._algorithms[algorithm_id]
        except (KeyError, TypeError) as exc:
            raise MindMapError(f"未注册布局算法：{algorithm_id}") from exc

    def list(self) -> tuple[LayoutInfo, ...]:
        """按注册顺序返回菜单元信息副本，调用方不能修改注册表的默认配置。"""
        return tuple(deepcopy(info) for info in self._info.values())

    def info(self, algorithm_id: str) -> LayoutInfo:
        """取得单个算法的元信息副本。"""
        self.get(algorithm_id)
        return deepcopy(self._info[algorithm_id])

    def validate_config(self, config: LayoutConfig) -> None:
        """先校验通用结构，再隔离调用具体算法的配置校验。"""
        if not isinstance(config, LayoutConfig):
            raise MindMapError("布局配置必须为 LayoutConfig")
        config.validate()
        algorithm = self.get(config.algorithm_id)
        info = self._info[config.algorithm_id]
        if info.directions and config.direction not in info.directions:
            raise MindMapError("当前布局算法不支持所选方向")
        if config.compact and not info.supports_compact:
            raise MindMapError("当前布局算法不支持紧凑模式")
        try:
            algorithm.validate_config(deepcopy(config))
        except MindMapError:
            raise
        except Exception as exc:
            raise MindMapError(f"布局配置校验失败：{info.name}：{exc}") from exc

    def layout(
        self,
        document: MindMapDocument,
        measurements: Mapping[str, Point] | None = None,
        *,
        expand_all: bool = False,
        focus_id: str | None = None,
    ) -> LayoutResult:
        """调度并校验布局；按注册时的只读约定决定是否复制输入，默认隔离扩展。"""
        document.validate()
        self.validate_config(document.layout)
        algorithm = self.get(document.layout.algorithm_id)
        try:
            if self._copy_inputs[document.layout.algorithm_id]:
                layout_document, sizes = deepcopy(document), deepcopy(measurements)
            else:
                layout_document, sizes = document, measurements
            result = algorithm.layout(layout_document, sizes, expand_all=expand_all, focus_id=focus_id)
        except MindMapError:
            raise
        except Exception as exc:
            raise MindMapError(f"布局计算失败：{exc}") from exc
        if not isinstance(result, LayoutResult):
            raise MindMapError("布局算法必须返回 LayoutResult")
        # 在交给渲染层之前验证公共几何契约，避免错误结果先提交到界面后才触发 Qt 异常。
        try:
            root = focus_id or document.root_id
            if root not in result.nodes or not result.nodes.keys() <= document.nodes.keys():
                raise MindMapError("布局结果包含无效节点或缺少可见根节点")
            for rect in (*result.nodes.values(), result.bounds):
                if (
                    not all(
                        isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v)
                        for v in (rect.x, rect.y, rect.width, rect.height)
                    )
                    or min(rect.width, rect.height) <= 0
                ):
                    raise MindMapError("布局矩形必须包含有效坐标和正有限尺寸")
            parents = document.parents()
            for key, curve in result.branches.items():
                if key not in result.nodes or parents.get(key) not in result.nodes:
                    raise MindMapError("布局分支引用不可见节点")
                _validate_branch(curve)
        except MindMapError:
            raise
        except Exception as exc:
            raise MindMapError(f"布局结果无效：{exc}") from exc
        return result


def _validate_branch(branch: Curve | BranchPath) -> None:
    """进入任何渲染后端前检查公共路径契约，同时兼容已有四点 Curve 扩展。"""
    if not isinstance(branch, (Curve, BranchPath)):
        raise MindMapError("布局分支必须为 Curve 或 BranchPath")
    if not isinstance(branch.color, str) or not branch.color:
        raise MindMapError("布局分支必须包含颜色")
    if isinstance(branch, Curve):
        if len(branch.points) != 4:
            raise MindMapError("布局分支必须为带颜色的三次贝塞尔曲线")
        points = branch.points
    else:
        if (
            not isinstance(branch.width, (int, float))
            or isinstance(branch.width, bool)
            or not isfinite(branch.width)
            or branch.width <= 0
        ):
            raise MindMapError("布局分支线宽必须为正有限数值")
        if not isinstance(branch.commands, tuple) or not branch.commands:
            raise MindMapError("布局分支必须包含不可变路径指令")
        if not isinstance(branch.commands[0], PathCommand) or branch.commands[0].op != "M":
            raise MindMapError("布局分支必须从 M 指令开始")
        points = [branch.start, branch.end]
        for command in branch.commands:
            if (
                not isinstance(command, PathCommand)
                or command.op not in ("M", "L", "C", "Z")
                or not isinstance(command.points, tuple)
                or len(command.points) != {"M": 1, "L": 1, "C": 3, "Z": 0}[command.op]
            ):
                raise MindMapError("布局分支路径指令或点数无效")
            points.extend(command.points)
        if branch.arrow is not None:
            if not isinstance(branch.arrow, tuple) or len(branch.arrow) != 3:
                raise MindMapError("布局分支箭头必须包含三个顶点")
            points.extend(branch.arrow)
        if any(not isinstance(point, tuple) for point in points):
            raise MindMapError("布局分支坐标必须为不可变元组")
    if any(
        len(point) != 2
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v) for v in point)
        for point in points
    ):
        raise MindMapError("布局分支坐标无效")


class DocumentImporter(Protocol):
    """从二进制流读取完整文档；解析失败不允许修改任何宿主文档。"""

    def read(self, stream: BinaryIO, *, options: Mapping[str, Any]) -> MindMapDocument:
        """读取后返回独立文档，结构由公共调度入口再次校验。"""
        ...


class DocumentExporter(Protocol):
    """将文档快照写入二进制流；临时文件及原子替换由文件服务负责。"""

    def write(self, document: MindMapDocument, stream: BinaryIO, *, options: Mapping[str, Any]) -> None:
        """按选项输出当前格式，不得修改文档或管理宿主保存标记。"""
        ...


@dataclass(frozen=True)
class FormatInfo:
    """格式的稳定标识、展示名称、带点扩展名以及是否完整保留文档。"""

    id: str
    name: str
    extensions: tuple[str, ...]
    lossless: bool = False


@dataclass(frozen=True)
class FormatRegistration:
    """允许单向能力；完整往返格式才可作为主文档保存目标。"""

    info: FormatInfo
    importer: DocumentImporter | None = None
    exporter: DocumentExporter | None = None

    @property
    def can_save(self) -> bool:
        return self.info.lossless and self.importer is not None and self.exporter is not None


class FormatRegistry:
    """统一管理文档导入、导出与能力查询；不依赖具体编解码器和文件系统。"""

    def __init__(self):
        self._formats: dict[str, FormatRegistration] = {}

    def register(
        self,
        info: FormatInfo,
        *,
        importer: DocumentImporter | None = None,
        exporter: DocumentExporter | None = None,
    ) -> None:
        """注册格式元信息及至少一种能力；相同扩展名可共存，由显式标识消歧。"""
        if not isinstance(info, FormatInfo):
            raise MindMapError("格式必须提供 FormatInfo")
        _validate_identity(info.id, info.name)
        if info.id in self._formats:
            raise MindMapError(f"格式标识重复：{info.id}")
        if importer is None and exporter is None:
            raise MindMapError("格式必须提供导入器或导出器")
        if (
            not isinstance(info.extensions, tuple)
            or not info.extensions
            or any(
                not isinstance(ext, str)
                or not ext.startswith(".")
                or len(ext) < 2
                or ext != ext.lower()
                or any(c in ext for c in ("/", "\\", " ", "*", ";", "(", ")"))
                for ext in info.extensions
            )
        ):
            raise MindMapError("格式扩展名必须为带点的小写后缀")
        if not isinstance(info.lossless, bool):
            raise MindMapError("格式完整保留标记必须为布尔值")
        self._formats[info.id] = FormatRegistration(deepcopy(info), importer, exporter)

    def get(self, format_id: str) -> FormatRegistration:
        """返回格式及能力，不按未知标识猜测其他格式。"""
        try:
            return self._formats[format_id]
        except (KeyError, TypeError) as exc:
            raise MindMapError(f"未注册文档格式：{format_id}") from exc

    def list(self) -> tuple[FormatRegistration, ...]:
        """按注册顺序枚举，宿主据此构建打开、保存和导出菜单。"""
        return tuple(self._formats.values())

    def resolve(
        self, path: str | PurePath, *, format_id: str | None = None, writing: bool = False
    ) -> FormatRegistration:
        """显式标识优先；否则按大小写无关的完整后缀匹配，多个候选时拒绝猜测。"""
        if format_id is not None:
            candidates = [self.get(format_id)]
        else:
            filename = str(path).lower()
            candidates = [
                entry
                for entry in self._formats.values()
                if any(filename.endswith(ext) for ext in entry.info.extensions)
                and (entry.exporter is not None if writing else entry.importer is not None)
            ]
        if not candidates:
            raise MindMapError(f"未找到对应的文档格式：{path}")
        if len(candidates) > 1:
            raise MindMapError("文档扩展名存在歧义，请明确指定格式标识")
        entry = candidates[0]
        if (entry.exporter if writing else entry.importer) is None:
            raise MindMapError(f"格式不支持{'导出' if writing else '导入'}：{entry.info.name}")
        return entry

    def read(self, stream: BinaryIO, *, format_id: str, options=None) -> MindMapDocument:
        """调用导入器并校验结果，统一包装第三方错误，不接受部分解析文档。"""
        entry = self.resolve("", format_id=format_id)
        try:
            document = entry.importer.read(stream, options=deepcopy(options or {}))
            if not isinstance(document, MindMapDocument):
                raise MindMapError("导入器必须返回 MindMapDocument")
            document.validate()
            return document
        except MindMapError:
            raise
        except Exception as exc:
            raise MindMapError(f"导入文档失败：{entry.info.name}：{exc}") from exc

    def write(self, document, stream: BinaryIO, *, format_id: str, options=None) -> None:
        """向导出器传入独立快照，调用方文档和历史不受扩展修改影响。"""
        entry = self.resolve("", format_id=format_id, writing=True)
        document.validate()
        try:
            entry.exporter.write(deepcopy(document), stream, options=deepcopy(options or {}))
        except MindMapError:
            raise
        except Exception as exc:
            raise MindMapError(f"导出文档失败：{entry.info.name}：{exc}") from exc


def _validate_identity(identifier: str, name: str) -> None:
    if not isinstance(identifier, str) or not identifier.strip():
        raise MindMapError("扩展标识必须为非空字符串")
    if not isinstance(name, str) or not name.strip():
        raise MindMapError("扩展名称必须为非空字符串")
