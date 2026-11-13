# 思维导图应用入口

`mindmap-app` 提供 `mindmap` 启动命令，直接构造支持树页和节点图的导图应用及桌面运行时。

## 启动

```powershell
uv sync --locked --all-packages --group dev
uv run --locked --package mindmap-app mindmap
```

也可使用 `uv run --locked --package mindmap-app python -m mindmap_app`。命令末尾可附加 `.mindmap` 文件路径，`--help` 显示完整参数。

## 装配

`build_window(storage_dir=None, theme=None, restore=True, options=None)` 创建窗口。`options` 可注入 `layout_registry`、`format_registry` 和 `page_registry`。产品工厂只创建一个导图控制器，扩展通过 `mindmap.node_pages` 发现。

## 本机配置

默认配置目录为系统配置目录下的 `mindmap`。`--state-dir` 可指定隔离目录。

`mindmap-state-v2.json` 保存窗口、标签、分屏、导航和最近访问，视图记录不携带业务类型字段。`mindmap-settings-v1.json` 保存窗口与导图偏好。文档正文和节点扩展数据统一保存到 `.mindmap`。

历史窗口与导图配置由[迁移层](src/mindmap_app/migration.py)只读转换。图的使用方式见[节点图使用说明](../../docs/节点图.md)。
