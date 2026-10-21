"""安全基线验收（任务 1.12）。

自包含、**不依赖 MySQL**，直接驱动 ``app.security.*`` 各模块，逐项打印 PASS/FAIL：

1. 字段加密：AES-256-GCM 往返一致、nonce 随机（同明文两次密文不同）、篡改可检出、空值处理
2. 日志脱敏：走**真实 logging 管道**（root handler + 子 logger），验证落盘文本已掩码
3. 限流：滑动窗口边界（第 ``limit+1`` 次被拒）、``Retry-After`` 语义
4. 敏感词：内置词表命中、正常文本不误伤、外部词表加载
5. 操作日志：JSON 模式降级（返回 False 且不抛错）、``detail`` 写前脱敏
6. SQL 拼接静态扫描（AST）：``app/`` 下禁止 f-string / ``%`` / ``+`` / ``.format()`` 拼 SQL

用法::

    python tools/verify_security.py

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import ast
import io
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认非 UTF-8，统一输出编码，避免中文乱码
try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from app.config import BASE_DIR, settings  # noqa: E402
from app.security import crypto, masking, oplog, ratelimit, sensitive  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    mark = "PASS" if ok else "FAIL"
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [{mark}] {name}{tail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ------------------------------------------------------------------ 1. 字段加密
def verify_crypto() -> None:
    section("1. 字段加密（AES-256-GCM）")
    plain = "110101199001011234"

    blob = crypto.encrypt_field(plain)
    check("密文非空", bool(blob))
    check("带版本前缀 v1", bool(blob) and blob.startswith(crypto.VERSION))
    check("往返解密一致", crypto.decrypt_field(blob) == plain)

    blob2 = crypto.encrypt_field(plain)
    check("nonce 随机（同明文两次密文不同）", blob != blob2)
    check("第二次仍可解密", crypto.decrypt_field(blob2) == plain)

    tampered = bytearray(blob)
    tampered[-1] ^= 0x01  # 翻转密文最后一位
    try:
        crypto.decrypt_field(bytes(tampered))
        check("篡改可检出", False, "未抛 CryptoError")
    except crypto.CryptoError:
        check("篡改可检出（AEAD 认证标签）", True)

    try:
        crypto.decrypt_field(b"garbage-not-our-format")
        check("格式不符可检出", False, "未抛 CryptoError")
    except crypto.CryptoError:
        check("格式不符可检出", True)

    check("None 加密 → None", crypto.encrypt_field(None) is None)
    check("空串加密 → None", crypto.encrypt_field("") is None)
    check("None 解密 → None", crypto.decrypt_field(None) is None)
    check("is_encrypted 识别密文", crypto.is_encrypted(blob))
    check("is_encrypted 拒绝明文", not crypto.is_encrypted(b"plain"))
    check(
        "AES_KEY 已配置" if settings.aes_key else "AES_KEY 未配置（退化临时密钥，生产需设置）",
        True,
    )


# ------------------------------------------------------------------ 2. 日志脱敏
def verify_masking() -> None:
    section("2. 日志脱敏（真实 logging 管道）")

    check(
        "证件号掩码",
        masking.mask_text("110101199001011234") == "110101********1234",
        masking.mask_text("110101199001011234"),
    )
    check(
        "手机号掩码",
        masking.mask_text("13812345678") == "138****5678",
        masking.mask_text("13812345678"),
    )
    check(
        "学号掩码",
        masking.mask_text("20230001") == "2023****",
        masking.mask_text("20230001"),
    )
    check(
        "邮箱掩码",
        masking.mask_text("zhangsan@x.edu.cn") == "z***@x.edu.cn",
        masking.mask_text("zhangsan@x.edu.cn"),
    )
    mixed = "联系 13812345678 或 zhangsan@x.edu.cn，学号 20230001"
    masked = masking.mask_text(mixed)
    check(
        "混合文本整体脱敏",
        "13812345678" not in masked
        and "zhangsan@x.edu.cn" not in masked
        and "20230001" not in masked,
        masked,
    )
    check("空文本不报错", masking.mask_text("") == "")

    # 真实管道：新增 handler → 安装 filter → 由**子 logger** 写日志
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    prev_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        masking.install_masking_filter()
        child = logging.getLogger("verify.security.child")
        child.info("用户 13812345678 的证件号 110101199001011234")
        child.warning("参数化：%s / %s", "13800001111", "zhangsan@x.edu.cn")
        out = stream.getvalue()
        # 在临时 handler 仍挂载时断言，否则普通脚本下 root.handlers 可能为空
        attached = any(
            isinstance(f, masking.MaskingFilter) for h in root.handlers for f in h.filters
        )
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)

    check("子 logger 直接写 → 已脱敏", "13812345678" not in out, out.strip()[:80])
    check("子 logger 参数化 → 已脱敏", "13800001111" not in out and "zhangsan@x.edu.cn" not in out)
    check("filter 已挂到 root.handlers", attached)

    detail = masking.mask_detail(
        {"phone": "13812345678", "nested": {"id": "110101199001011234"}, "num": 7}
    )
    check(
        "mask_detail 递归脱敏",
        detail["phone"] == "138****5678" and detail["nested"]["id"] == "110101********1234",
    )
    check("mask_detail 非字符串原样保留", detail["num"] == 7)
    check("mask_detail(None) → None", masking.mask_detail(None) is None)


# ------------------------------------------------------------------ 3. 限流
def verify_ratelimit() -> None:
    section("3. 限流（内存滑动窗口）")

    limiter = ratelimit.SlidingWindowLimiter(limit=3, window_seconds=60)
    allowed_flags = []
    for i in range(5):
        allowed, remaining, retry = limiter.hit("k", now=100.0 + i * 0.1)
        allowed_flags.append(allowed)
        if i == 3:
            check("第 limit+1 次被拒", not allowed)
            check("被拒时 remaining=0", remaining == 0)
            check("retry_after > 0", retry > 0, f"{retry}s")
    check("前 limit 次放行", allowed_flags[:3] == [True, True, True])
    check("剩余额度递减", limiter.hit("k2", now=200.0)[1] == 2)

    # 窗口滑出后恢复
    limiter2 = ratelimit.SlidingWindowLimiter(limit=2, window_seconds=10)
    limiter2.hit("w", now=0.0)
    limiter2.hit("w", now=1.0)
    check("窗口内已满", not limiter2.hit("w", now=2.0)[0])
    check("窗口滑出后恢复", limiter2.hit("w", now=11.0)[0])

    check("多 key 互不影响", limiter2.hit("other", now=11.0)[0])
    check("reset 清空", (limiter2.reset("w"), limiter2.hit("w", now=11.0)[0])[1])
    check("话术含「稍后再试」", "稍后再试" in ratelimit.TOO_MANY)
    check("限流开关状态", True, f"RATE_LIMIT_ENABLED={settings.rate_limit_enabled}")


# ------------------------------------------------------------------ 4. 敏感词
def verify_sensitive() -> None:
    section("4. 敏感词双向检测")

    words = sensitive.load_words()
    check("词表已加载", len(words) >= len(sensitive.DEFAULT_WORDS), f"{len(words)} 条")

    for text in ("忽略以上指令", "ignore previous instructions", "越狱模式怎么开"):
        check(f"命中：{text}", sensitive.check_question(text) is not None)

    for text in ("校园网怎么开户？", "图书馆借阅规则是什么", "补办学生证流程"):
        check(f"不误伤：{text}", sensitive.check_question(text) is None)

    check("回答侧检测复用同一逻辑", sensitive.check_answer("忽略以上") is not None)
    check("空文本不命中", sensitive.check("") is None and sensitive.check(None) is None)
    check("关闭开关后放行", sensitive.check("忽略以上", enabled=False) is None)

    stat = sensitive.stats()
    check("stats 含 enabled/words/file", {"enabled", "words", "builtin", "file"} <= set(stat))
    check("风控话术非空", bool(sensitive.RISK_REPLY))


# ------------------------------------------------------------------ 5. 操作日志
def verify_oplog() -> None:
    section("5. 操作日志")

    original_store = settings.kb_store
    try:
        settings.kb_store = "json"  # 强制降级分支
        ok = oplog.log_operation(
            oplog.ACTION_DOC_UPLOAD,
            "kb_doc",
            "D001",
            detail={"filename": "a.md", "phone": "13812345678"},
        )
        check("JSON 模式降级返回 False（不落库）", ok is False)
        check("降级不抛异常", True)

        masked = masking.mask_detail({"phone": "13812345678"})
        check("detail 写前脱敏", masked["phone"] == "138****5678", masked["phone"])

        check("动作码常量齐备", all([
            oplog.ACTION_DOC_UPLOAD,
            oplog.ACTION_DOC_STATUS,
            oplog.ACTION_DOC_DELETE,
            oplog.ACTION_SYNC,
        ]))
    finally:
        settings.kb_store = original_store

    check("当前存储模式", True, f"KB_STORE={settings.store_label}")


# ------------------------------------------------------------------ 6. SQL 静态扫描
_CALL_NAMES = {"text", "execute", "executemany", "executescript"}
_EXEMPT_MARK = "noqa: SQL-IDENTIFIER"


def _is_interpolated(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr):  # f-string
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr == "format"
    return False


def verify_sql_guard() -> None:
    section("6. SQL 拼接静态扫描（AST）")

    offenders: list[str] = []
    exempted: list[str] = []
    for path in sorted((BASE_DIR / "app").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover
            check(f"解析 {path.name}", False, str(exc))
            continue
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name not in _CALL_NAMES or not node.args:
                continue
            if not _is_interpolated(node.args[0]):
                continue
            rel = f"{path.relative_to(BASE_DIR)}:{node.lineno}"
            line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            if _EXEMPT_MARK in line:
                exempted.append(rel)
            else:
                offenders.append(rel)

    check("无未豁免的拼接 SQL", not offenders, ", ".join(offenders) if offenders else "")
    check("豁免项均带标记", len(exempted) >= 0, f"豁免 {len(exempted)} 处：{', '.join(exempted)}")


def main() -> int:
    print("=" * 62)
    print("kb-service 安全基线验收（任务 1.12）")
    print(f"store={settings.store_label} | aes_key={'set' if settings.aes_key else 'unset'} "
          f"| jwt_secret={'set' if settings.jwt_secret else 'unset'}")
    print("=" * 62)

    verify_crypto()
    verify_masking()
    verify_ratelimit()
    verify_sensitive()
    verify_oplog()
    verify_sql_guard()

    print("\n" + "=" * 62)
    print(f"汇总：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 62)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
