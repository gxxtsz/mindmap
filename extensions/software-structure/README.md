# 软件架构节点详情页

`mindmap-extension-software-structure` 为导图节点提供软件架构专用详情，依赖 `mindmap-qt-editor` 的节点页面协议，全部编辑通过所属导图的事务提交。

## 打开与保存

选中导图节点，在“扩展详情”中添加或打开“软件架构”。专用页面在当前导图标签内展开，顶部路径及返回按钮可回到原节点。每个节点拥有独立架构内容。

在页内点击“导入架构数据…”读取 `.software-structure` 文件。来源文件只读，全部 JSON 数据（包括自定义字段）存入节点，随后随 `.mindmap` 自动保存。再次导入替换本节点内容，可整体撤销。页内“下载格式模板…”提供可编辑的导入格式示例。

## 浏览

- 滚轮缩放，右键拖动平移；页脚提供适应窗口及恢复 100% 的按钮。
- 点击模块高亮直接入边和出边；循环依赖用虚线表示。
- 页内“模块接口”显示模块职责和接口，类成员可以展开。
- 页内“连线筛选”可隐藏指定模块的入边和出边，批量操作与单项勾选都参与导图撤销历史。
- 临时显示全部依赖只影响本页视口；持久筛选在共享同一导图的标签间同步。
- 导图只读时仍可浏览，禁止导入与修改持久筛选。

## 文件格式

文件扩展名为 `.software-structure`，内容为 UTF-8 编码的 JSON 对象，可带 BOM。每份文件的顶层包含 `format_description`，说明各字段的类型、用途和约束。缺少该字段也可以导入，不补写或更改来源文件。

以下示例表示「应用」依赖「核心」，并说明「核心」提供的一个接口。筛选时隐藏与「核心」相连的线。

```json
{
  "schema_version": 1,
  "format_description": {
    "file": "文件扩展名为 .software-structure，内容为 UTF-8 编码的 JSON 对象，可带 BOM。",
    "schema_version": "必填整数，当前支持版本 1。",
    "format_description": "必填对象，键为字段名或说明主题，值为说明文本。该字段供阅读文件时参考。",
    "modules": "必填数组，可为空。每个模块必填字符串 id 和 name，二者至少包含一个非空白字符。id 在文档中唯一，按原文匹配并区分大小写。name 为显示名称，description 为描述模块职责的可选字符串。",
    "module_dependencies": "可选数组，省略时按空数组读取。每项的 source 和 target 分别引用依赖方和被依赖方的模块 ID，同一有序对只出现一次。支持自依赖和循环依赖。",
    "module_apis": "可选数组，省略时按空数组读取。每组必填 module_id 和 apis 数组。module_id 引用已声明的模块 ID，每个模块最多一组，apis 可以为空。",
    "apis": "接口和类成员必填字符串 name 和 description，二者至少包含一个非空白字符。省略 kind 表示普通接口，kind 为 class 表示类。类可包含 members 数组，成员与接口采用相同格式。",
    "hidden_edge_module_ids": "可选数组，包含已声明且互不重复的模块 ID。连线的任一端出现在数组中时，该连线会被隐藏。空数组表示显示全部连线。",
    "extensions": "版本 1 允许额外字段。"
  },
  "modules": [
    {"id": "app", "name": "应用"},
    {"id": "core", "name": "核心", "description": "提供业务基础能力"}
  ],
  "module_dependencies": [
    {"source": "app", "target": "core"}
  ],
  "module_apis": [
    {
      "module_id": "core",
      "apis": [{"name": "读取项目", "description": "打开已保存的项目，供其他模块继续处理。"}]
    }
  ],
  "hidden_edge_module_ids": ["core"]
}
```

| 顶层字段 | 类型与约束 |
| --- | --- |
| `schema_version` | 必填整数。当前支持 `1`。 |
| `format_description` | 必填对象。键为字段名或说明主题，值为说明文本。 |
| `modules` | 必填数组。每项为模块对象。空数组显示空图。 |
| `module_dependencies` | 可选数组。每项为依赖对象，省略时按空数组读取。 |
| `module_apis` | 可选数组。每项按模块 ID 提供接口说明，缺省时也可打开文档。 |
| `hidden_edge_module_ids` | 可选数组。每项是唯一且已声明的模块 ID。勾选按模块顺序保存，清除筛选时保存空数组。 |

- 模块对象的 `id` 和 `name` 是必填字符串，至少包含一个非空白字符。`id` 在整个文档中唯一，按原文匹配，区分大小写。`description` 为可选字符串，用于说明模块职责。
- 缺省 `hidden_edge_module_ids` 时，打开旧文件会读取模块对象中为 `true` 的 `is_public_dependency` 作为初始勾选。页面修改筛选时把新字段写入所属节点，保留其他业务字段；来源文件的字节、BOM 和换行保持不变。
- 依赖对象的 `source` 和 `target` 是必填字符串，分别引用依赖方和被依赖方的模块 ID。每组有序的 `source` 和 `target` 只能出现一次。允许孤立模块、双向依赖、自依赖和其他循环依赖。
- 接口组必填 `module_id` 和 `apis` 数组。`module_id` 引用已声明的模块 ID，每个模块最多一组。缺少分组表示文件未提供该模块的接口说明，`apis` 为空表示暂无符合收录范围的接口。
- 接口和类成员的 `name`、`description` 为必填字符串，至少包含一个非空白字符。`kind` 省略时表示普通接口，填 `"class"` 时表示类。类可选填 `members` 数组，成员沿用相同格式。界面按文件顺序显示名称和用途。节点筛选事务保留接口数据。
- 版本 1 的读取器校验已识别字段，允许额外字段。不兼容的格式变更应递增 `schema_version`。
