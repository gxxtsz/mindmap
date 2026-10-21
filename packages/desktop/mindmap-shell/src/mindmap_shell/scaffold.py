"""从随 wheel 交付的模板生成独立项目；不执行模板文本，也不覆盖已有目录。"""

import argparse
import json
import keyword
import re
import shutil
import tempfile
from pathlib import Path


def generate_project(destination, *, project_name="my-desktop-app", module_name=None,
                     title="我的桌面应用", framework_root=None):
    """生成可独立维护的 uv 项目，返回绝对目录。

    destination 为尚不存在的目标目录；返回路径归调用方所有，生成器不管理后续
    修改或运行。project_name、module_name 和 title 非法时抛出 ValueError，目标
    已存在时抛出 FileExistsError，文件读写失败保留原有目标并传播 OSError。

    framework_root 指向 mindmap 仓库时，将两项依赖显式绑定到本地路径，未发布到
    包索引也能立即运行。省略时自动检测当前源码工作区；安装版无法检测时保留
    标准版本依赖，发布或安装本地 wheel 后由调用方正常解析。
    """
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", project_name):
        raise ValueError("项目名应为 2 至 64 个小写字母、数字或连字符")
    module_name = module_name or project_name.replace("-", "_")
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", module_name) or keyword.iskeyword(module_name):
        raise ValueError("模块名应为 2 至 64 个小写字母、数字或下划线")
    if not title.strip() or len(title) > 120:
        raise ValueError("应用名称应为 1 至 120 个字符")
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise FileExistsError("目标目录已经存在；生成器不会覆盖已有文件")
    source = Path(__file__).parent / "project_template"
    if framework_root is None:
        # 包按子系统分组后目录深度不再固定；仅将包含 uv workspace 的祖先当作源码根。
        import tomllib

        for candidate in Path(__file__).resolve().parents:
            metadata = candidate / "pyproject.toml"
            if metadata.is_file() and "workspace" in tomllib.loads(metadata.read_text("utf-8")).get("tool", {}).get("uv", {}):
                framework_root = candidate
                break
    if framework_root is not None:
        framework_root = Path(framework_root).resolve()
        if not all((framework_root / path / "pyproject.toml").is_file()
                   for path in ("packages/desktop/mindmap-docking", "packages/desktop/mindmap-shell")):
            raise ValueError("框架目录必须包含 packages/desktop 下的停靠与外壳成员")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="mindmap-new-", dir=destination.parent)).resolve()
    try:
        for item in source.rglob("*"):
            if not item.is_file():
                continue
            relative = item.relative_to(source).as_posix().replace("my_desktop_app", module_name)
            output = staging / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            text = item.read_text("utf-8")
            text = text.replace("my_desktop_app", module_name).replace("my-desktop-app", project_name)
            text = text.replace('"我的桌面应用"', json.dumps(title, ensure_ascii=False))
            text = text.replace("# 我的桌面应用", "# " + title)
            if relative == "pyproject.toml" and framework_root is not None:
                text += "\n[tool.uv.sources]\n"
                for name in ("mindmap-shell", "mindmap-docking"):
                    path = json.dumps((framework_root / "packages/desktop" / name).as_posix())
                    text += f"{name} = {{ path = {path}, editable = true }}\n"
            output.write_text(text, "utf-8")
        # 发布前再次确认目标尚不存在，完整写入的临时目录才可提交为最终工程。
        if destination.exists():
            raise FileExistsError("生成期间目标目录已被其他程序创建")
        staging.rename(destination)
    finally:
        # 仅清理本次 mkdtemp 在明确父目录中创建的目录，拒绝越过目标父目录边界。
        if staging.exists() and staging.is_relative_to(destination.parent) and staging.name.startswith("mindmap-new-"):
            shutil.rmtree(staging)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description="使用内置桌面外壳生成独立应用项目")
    parser.add_argument("destination", help="尚不存在的目标目录")
    parser.add_argument("--name", default="my-desktop-app", help="Python 分发包名")
    parser.add_argument("--module", help="Python 导入模块名")
    parser.add_argument("--title", default="我的桌面应用", help="窗口标题")
    parser.add_argument("--framework-root", type=Path, help="可选的 mindmap 仓库目录")
    args = parser.parse_args(argv)
    try:
        output = generate_project(args.destination, project_name=args.name, module_name=args.module,
                                  title=args.title, framework_root=args.framework_root)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"项目已生成：{output}\n进入该目录，执行 uv sync，再执行 uv run {args.name}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
