"""前后端富文本渲染的一致性用例（任务 1.9）。

同一批 Markdown 输入同时喂给两侧：

- 后端 ``app.core.richtext.render_html()``
- 前端 ``static/app.js`` 的 ``renderContent()``（经 Node 加载，见 ``richtext_probe.mjs``）

两侧输出应**逐字符一致**。本模块只放数据，供 ``tests/test_richtext.py`` 与
``tools/verify_richtext.py`` 共用，避免两份用例各自漂移。

覆盖：结构语法（标题/列表/代码/引用/表格/分隔线/图片）、行内语法（加粗/斜体/
删除线/行内代码/引用上标/链接）与 XSS 攻击面（脚本标签、协议注入、属性注入、
大小写与制表符混淆）。
"""

from __future__ import annotations

#: (用例名, Markdown 输入)
CASES: list[tuple[str, str]] = [
    # ---- 结构语法 ----
    ("标题与段落", "## 校园网开户流程\n\n请按以下步骤办理。"),
    ("多级标题", "# 一级\n\n### 三级"),
    ("无序列表", "- 材料：学生证\n- 费用：免费"),
    ("有序列表", "1. 登录自助服务\n2. 携带证件到服务大厅"),
    ("围栏代码块", "```bash\ncurl https://example.edu.cn/api\n```"),
    ("无语言代码块", "```\nplain text\n```"),
    ("引用块", "> 注意：账号密码请通过官方渠道办理。"),
    ("分隔线", "上文\n\n---\n\n下文"),
    ("表格（含对齐）", "| 项目 | 说明 | 费用 |\n|:---|:---:|---:|\n| 开户 | 首次开通 | 0 元 |\n| 补办 | 挂失后 | 20 元 |"),
    ("表格（无外框管道）", "项目 | 费用\n--- | ---\n开户 | 0 元"),
    ("块级图片", "![校园网拓扑图](/static/topo.png)"),
    ("段落内换行", "第一行\n第二行"),
    # ---- 行内语法 ----
    ("加粗与引用上标", "具体见 **信息网络中心** 通知[1]。"),
    ("连续引用上标", "结论[1][2]已确认。"),
    ("斜体与删除线", "这是 *强调* 与 ~~旧版~~ 内容。"),
    ("行内代码", "执行 `pip install kb-service` 即可。"),
    ("安全外链", "详见 [自助服务](https://net.example.edu.cn)。"),
    ("相对路径链接", "详见 [切片明细](/api/docs/D001/chunks)。"),
    ("混合富文本", "## 结论\n\n- 依据 **政策文件**[1]\n- 详见 [官网](https://www.example.edu.cn)\n\n| 项 | 值 |\n|---|---|\n| 状态 | 已发布 |"),
    # ---- XSS 攻击面 ----
    ("脚本标签", "<script>alert(1)</script>"),
    ("img onerror 注入", '<img src=x onerror="alert(2)">'),
    ("javascript 链接", "[点我](javascript:alert(3))"),
    ("javascript 图片", "![x](javascript:alert(4))"),
    ("大小写混淆协议", "[点我](JaVaScRiPt:alert(5))"),
    ("制表符混淆协议", "[点我](java\tscript:alert(6))"),
    ("data 协议", "[点我](data:text/html;base64,PHNjcmlwdD4=)"),
    ("vbscript 协议", "[点我](vbscript:msgbox(7))"),
    ("属性引号注入", '" onmouseover="alert(8)'),
    ("HTML 实体与引号", "& < > \" '"),
    ("表格单元格内注入", "| 项 | 值 |\n|---|---|\n| a | <script>alert(9)</script> |"),
    # ---- 边界 ----
    ("空文本", ""),
    ("纯空白", "   \n  "),
    ("未闭合代码块", "```bash\ncurl a\n"),
    ("未识别语法原样转义", "这里是 @@#$%^&*() 未识别符号"),
]
