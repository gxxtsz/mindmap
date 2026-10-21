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
默认工厂将受控的内置树布局注册为只读实现，避免布局前再次深拷贝整图；第三方注册仍默认隔离输入。
同一实例缓存最近一次几何计算，复用拖动试算、正式提交及相同尺寸下的布局；每次返回独立结果映射，调用方修改结果不会污染缓存。

内置树布局声明支持分支样式，节点矩形排布完成后由 `connectors` 统一生成 11 种连接线。
`route_branch` 提供单边路由；`route_branches` 按父主题设置批量路由并去除相同画笔的重复直主干。
左向镜像，向下转置，双向按实际左右位置分组；未配置样式时保持旧 `Curve` 及其控制点。
普通弧线、坡度曲线和圆角采用已恢复公式；主干比例、短边保护、箭头尺寸及第 11 类参数为独立重建。

## 正式画布增量几何

`mindmap_layouts.incremental.IncrementalTreeGeometry` 已接入正式画布，维护子树摘要、
持久化同级索引及分层视口查询，支持四方向、折叠、变尺寸和子树移动。
`snapshot()` 共享不可变树根，后续更新不会改写旧版本；`rectangle()` 按父链解析坐标，
`query()` 仅遍历与视口有关的区域，`all_rectangles()` 显式生成完整坐标。

`mindmap_layouts.incremental_result.IncrementalTreeLayout` 组合测量、分支样式摘要、局部路由缓存
及惰性 `LayoutResult`。`update()` 只接收变化节点，`result()` 返回稳定布局版本；
节点和连接线映射支持完整枚举，但交互期间应使用视口查询，避免主动展开全图数据。
连接线按父级局部坐标缓存，平移时仅更新世界原点，保留主干去重和绘制顺序。

默认注册表的 `TreeLayout` 继续返回原有完整结果，供扩展、聚焦和导出使用。
正式画布只为未被覆写的内置树布局启用增量服务，第三方算法保留隔离与完整校验。
首次加载、全局配置变化及完整输出仍需处理整图。
