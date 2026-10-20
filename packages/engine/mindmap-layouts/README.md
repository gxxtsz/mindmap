# mindmap-layouts

内置布局能力包，仅依赖 `mindmap-core`。一个确定性的树布局算法支持左、右、双向、向下四种方向。

```python
from mindmap_core import MindMapDocument
from mindmap_layouts import create_default_layout_registry

document = MindMapDocument.new("中心主题")
registry = create_default_layout_registry()
result = registry.layout(document)
```

真实图形界面应传入包含文字、标签及图片的完整节点尺寸。省略测量时使用近似值。
聚焦、临时展开和导出均不改变文档。每次工厂调用返回独立实例，宿主可以继续注册自定义算法。

内置树布局声明支持分支样式，节点矩形排布完成后由 `connectors` 统一生成 11 种连接线。
`route_branch` 提供单边路由；`route_branches` 按父主题设置批量路由并去除相同画笔的重复直主干。
左向镜像，向下转置，双向按实际左右位置分组；未配置样式时保持旧 `Curve` 及其控制点。
普通弧线、坡度曲线和圆角采用已恢复公式；主干比例、短边保护、箭头尺寸及第 11 类参数为独立重建。
