# workbench-runtime

通用窗口、插件生命周期、文档协调、命令路由、设置和会话恢复。不依赖导图引擎或任何编辑器业务包。架构、插件接入及兼容策略见仓库的《插件化工作台》文档。

仅安装本包和 `workbench-plugin-text` 即可使用纯文本工作台；在已安装环境中创建入口：

```python
from PySide6.QtWidgets import QApplication
from workbench_runtime import WorkbenchConfig, WorkbenchRuntimeWindow

app = QApplication([])
config = WorkbenchConfig(application_id="my-workbench", title="我的工作台")
window = WorkbenchRuntimeWindow(config=config)
window.show()
app.exec()
```

运行时通过 entry points 发现已安装插件。传入 `manifests=[]` 可创建没有编辑器的空工作台，`storage_dir` 可指定独立配置目录。

`WorkbenchConfig` 声明应用标识、标题和图标。默认目录是系统配置位置下的应用标识；具体产品有历史目录时必须显式注入 `storage_dir`。
`personal-workbench` 产品负责传入旧应用标识、原配置目录和只读迁移函数，运行时不内置这些历史名称。

本包的 `tests/` 覆盖插件生命周期、状态校验、文档去重和两阶段关闭。涉及真实文本编辑器的测试使用文本插件及通用宿主，不经过产品入口或导图插件。

使用根目录的 uv workspace 同步、测试和构建本包。代码接口与关键生命周期说明均使用中文。
