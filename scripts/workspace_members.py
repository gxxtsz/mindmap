"""从根配置发现真实工作区成员，构建验收不再根据包名猜测目录。"""

import tomllib
from pathlib import Path


def workspace_members(repository: Path) -> dict[str, tuple[Path, dict]]:
    """返回分发名到成员目录及项目元数据的映射。

    repository 必须为含 uv workspace 配置的仓库根目录。仅遍历 members 声明的
    路径，忽略没有项目元数据的分组项；重名成员抛出 ValueError，防止错误的
    wheel 覆盖。返回顺序按路径稳定排序，不包含根目录的虚拟项目或项目模板。
    """
    config = tomllib.loads((repository / "pyproject.toml").read_text("utf-8"))
    result = {}
    paths = {path for pattern in config["tool"]["uv"]["workspace"]["members"] for path in repository.glob(pattern)}
    for path in sorted(paths):
        metadata = path / "pyproject.toml"
        if not metadata.is_file():
            continue
        project = tomllib.loads(metadata.read_text("utf-8"))["project"]
        name = project["name"]
        if name in result:
            raise ValueError(f"工作区成员名称重复：{name}")
        result[name] = path, project
    return result
