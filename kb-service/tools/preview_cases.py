"""前后端关键词检索的共享用例（任务 1.11）。

``tools/preview_probe.mjs`` 在 Node 里跑 ``static/preview.js`` 的纯函数，
``tools/verify_preview.py`` 在 Python 里跑 ``app/services/doc_preview.py``，
两侧对同一批用例要求**逐字符一致**。

**用例限定在 BMP 内（不含代理对 / emoji）**：JS 字符串按 UTF-16 码元计数、
Python 按码点计数，非 BMP 字符的区间数值天然不同——但两侧各自被自己的渲染器
消费（JS 用 JS 的 index 切片，Python 用 Python 的 index 切片），所以这不构成缺陷，
只是**区间数值不可跨语言直接比较**。用例文件用 :func:`assert_bmp_only` 自我守卫。
"""

from __future__ import annotations

#: ``(haystack, term)`` → 期望的命中区间（由两侧各自计算并比对）
RANGE_CASES: list[tuple[str, str]] = [
    # --- 基础 ---
    ("校园网开户与报修", "校园网"),
    ("校园网开户与报修", "报修"),
    ("校园网开户与报修", "不存在"),
    ("校园网开户与报修", "校园网开户与报修"),
    # --- 大小写不敏感 ---
    ("ABCabc", "abc"),
    ("Hello World hello", "hello"),
    ("MiXeD CaSe", "mixed case"),
    # --- 重叠与步进 ---
    ("aaaa", "aa"),
    ("aaaaa", "aa"),
    ("ababab", "abab"),
    # --- 中文重复 ---
    ("测试测试测试", "测试"),
    ("一卡通挂失与补办", "挂失"),
    # --- 空值 / 边界 ---
    ("", "a"),
    ("a", ""),
    ("", ""),
    ("abc", "abcd"),
    ("abc", "abc"),
    (" abc ", "abc"),
    # --- 命中数上限（每个关键词最多 50 段）---
    ("a" * 120, "a"),
    ("测" * 80, "测"),
    # --- 含换行 / 表格 ---
    ("| 套餐 | 费用 |\n| 基础 | 10 |", "费用"),
    ("第一行\n第二行\n第三行", "第二行"),
]

#: 待验证的检索词串（``parse_terms`` 语义：分隔 / 去空 / 去重）
TERM_CASES: list[str] = [
    "",
    "   ",
    "校园网",
    "校园网 报修",
    "校园网  报修",
    "校园网 校园网",
    " 校园网 ",
    "一卡通\t补办",
    "一卡通\n补办",
    "a b c d e",
    "\u00a0一卡通\u00a0补办\u00a0",
]


def assert_bmp_only() -> None:
    """用例不得包含非 BMP 字符（否则 JS/Python 区间数值天然不同）。"""
    for hay, term in RANGE_CASES:
        for s in (hay, term):
            for ch in s:
                assert ord(ch) <= 0xFFFF, f"用例含非 BMP 字符 {ch!r}（{hay!r}, {term!r}）"
    for q in TERM_CASES:
        for ch in q:
            assert ord(ch) <= 0xFFFF, f"检索词用例含非 BMP 字符 {ch!r}（{q!r}）"


# ------------------------------------------------------------------ 图片缩略图（任务 1.10）
#: ``(docId, image, expect)``。``expect`` 的 ``must`` / ``must_not`` 为 HTML 子串断言。
#: 覆盖：原件图 / 内嵌图 / 图缺失降级 / 文件名含 HTML 的转义（XSS）。
IMAGE_CASES: list[tuple[str, dict, dict]] = [
    (
        "D001",
        {
            "key": "uploads/original/a1b2c3d4e5f60718.png",
            "name": "校园网开户公告.png",
            "role": "original",
            "exists": True,
            "width": 900,
            "height": 270,
            "size": 24939,
            "chunkSeqs": [],
            "error": None,
        },
        {
            "must": [
                "原件",
                "900×270",
                "24.4 KB",
                'target="_blank"',
                'rel="noopener"',
                "/api/docs/D001/image?key=uploads%2Foriginal%2Fa1b2c3d4e5f60718.png",
            ],
            "must_not": ["图片缺失", "undefined"],
        },
    ),
    (
        "D002",
        {
            "key": "uploads/original/ffffffffffffffff.png",
            "name": "ffffffffffffffff.png",
            "role": "embedded",
            "exists": True,
            "width": 320,
            "height": 200,
            "size": 4096,
            "chunkSeqs": [1, 3],
            "error": None,
        },
        {"must": ["内嵌图", "切片 #1 #3", "320×200", "4.0 KB"], "must_not": ["图片缺失"]},
    ),
    (
        "D002",
        {
            "key": "uploads/original/0000000000000000.png",
            "name": "0000000000000000.png",
            "role": "embedded",
            "exists": False,
            "width": None,
            "height": None,
            "size": 0,
            "chunkSeqs": [],
            "error": "原图不存在或不是文件",
        },
        {"must": ["图片缺失", "broken"], "must_not": ["<img ", "undefined"]},
    ),
    (
        "D003",
        {
            "key": "uploads/original/evil0000evil0000.png",
            "name": "<img src=x onerror=alert(1)>.png",
            "role": "embedded",
            "exists": True,
            "width": 16,
            "height": 16,
            "size": 512,
            "chunkSeqs": [],
            "error": None,
        },
        {
            # 文件名里的尖括号必须被转义，不能变成可执行的标签
            "must": ["&lt;img src=x onerror=alert(1)&gt;.png"],
            "must_not": ["<img src=x onerror=alert(1)>"],
        },
    ),
]


def assert_image_cases_consistent() -> None:
    """用例自身的一致性守卫：缺 ``exists`` 的必须带 ``error``。"""
    for doc_id, img, expect in IMAGE_CASES:
        assert doc_id, "图片用例缺少 docId"
        assert img.get("key"), f"图片用例缺少 key：{img}"
        assert "exists" in img, f"图片用例缺少 exists：{img}"
        assert "must" in expect, f"图片用例缺少 must 断言：{doc_id}"
        if not img["exists"]:
            assert img.get("error"), f"图缺失用例应带 error：{img}"
            assert any("图片缺失" in s for s in expect["must"]), "图缺失用例应断言降级文案"


__all__ = [
    "IMAGE_CASES",
    "RANGE_CASES",
    "TERM_CASES",
    "assert_bmp_only",
    "assert_image_cases_consistent",
]
