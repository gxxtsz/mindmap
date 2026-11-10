# workbench-plugin-text

基于 `QPlainTextEdit` 的工作台文本插件，支持 `.txt` 与 `.md` 纯文本，不提供 Markdown 预览。

新文档以“未命名”标签打开，首次保存选择路径。同文件视图共享 `QTextDocument` 和撤销历史，各自保留光标、选择与滚动位置。
新文件使用 UTF-8 和 LF；已有文件保留 UTF-8 BOM 与主要换行风格。解码失败拒绝打开，保存使用同目录临时文件和原子替换。

`tests/test_files.py` 验证编码、保存失败、共享视图及中文组合输入；`tests/test_view_state.py` 验证视口恢复。
测试通过通用运行时加载文本插件，不依赖产品入口或导图实现。跨插件场景在产品应用测试中维护。

架构、插件接入、兼容策略和验证流程见[工作台运行时](../../packages/workbench-runtime/README.md)。

使用根目录的 uv workspace 同步、测试和构建本包。代码接口与关键生命周期说明均使用中文。
