# 思维导图主体

思维导图是产品的唯一业务主体，负责文档、节点、页面导航和数据保存。桌面窗口提供标签与分屏等基础设施，软件架构等扩展只能作为节点专用详情页进入。

## 分层

| 目录 | 分发包 | 职责 |
| --- | --- | --- |
| [core](core/README.md) | `mindmap-core` | 模型、事务、历史、布局和原生格式，仅依赖标准库 |
| [qt-render](qt-render/README.md) | `mindmap-qt-render` | Qt 测量、静态绘制、图形导出 |
| [qt-editor](qt-editor/README.md) | `mindmap-qt-editor` | 交互画布、面板、节点页面注册表与容器 |
| [application](application/README.md) | `mindmap-application` | 必备主体装配、文档会话、页内导航和自动保存 |

## Python 接口

```python
from mindmap_core import MindMapEngine
from mindmap_qt_editor.canvas import MindMapWidget
from mindmap_qt_editor.node_pages import NodePageHost, NodePageProvider, NodePageRegistry, NodePageWidget
```

独立使用画布需要先创建 `QApplication`。`NodePageHost` 可以包装现有画布，在相同导图文档中打开专用详情页。扩展的业务内容只通过引擎事务修改；控件状态只进入本机视图快照。

完整桌面应用使用 `mindmap_application` 必备组件，由产品入口显式装配。应用上下文和本机会话只有导图主体，外部能力只能作为节点页面接入。

扩展实现和注册步骤见[架构与节点扩展](../docs/架构与节点扩展.md)。运行命令见[应用启动说明](../apps/mindmap-app/README.md#启动)。
