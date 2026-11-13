# 导图桌面内部协议

`mindmap-desktop-api` 定义唯一导图应用与桌面基础设施之间的内部接口，Python 导入名为 `mindmap_desktop_api`。节点扩展使用 `mindmap_qt_editor.node_pages`，不能取得桌面应用上下文。

## 直接装配

产品向 `DesktopRuntimeWindow` 传入 `application_factory`。工厂接收 `ApplicationContext`，返回实现 `MindMapApplication` 的导图控制器。控制器实现新建导图、打开导图和资源释放；没有编辑器注册表、业务清单或动态业务发现。

`ApplicationContext` 提供命令、界面、文档、偏好、会话状态和日志服务。`own()` 将订阅及控件清理纳入初始化回滚与正常退出，按登记的反序释放。控制器的 `dispose()` 应当幂等。

## 文档与标签

`DocumentHandle` 代表一份共享导图，持有路径、修改状态和保存能力。`EditorView` 代表独立标签，持有页面容器、视口与输入状态。

应用在成功读取、校验及构造后调用 `context.documents.register_view(view)`。多个标签可以共享同一文档；同一路径不能被两份不同文档占用。复制标签保留各自的视图状态，保存和关闭按共享文档去重。

`capture_state()` 仅返回可序列化的导航和显示状态。`view_state_changed(view.id)` 触发合并保存，文档正文始终存入 `.mindmap`。

## 界面与命令

`CommandSpec`、`PanelSpec`、`StatusItemSpec` 声明导图应用的界面资源。`MenuContribution` 使用 `MenuLocation` 的稳定标识填充菜单，显示标题由桌面控制；菜单回调失败会丢弃本组未完成的动作并记录错误。

标准编辑动作通过 `EditorView.edit_actions` 路由；节点专用详情页只暴露其支持的动作。主题、活动标签和布局订阅都由应用上下文拥有。

完整声明见[应用及文档协议](src/mindmap_desktop_api/contracts.py)和 [Qt 界面协议](src/mindmap_desktop_api/qt.py)。
