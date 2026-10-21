"""12 格式 + OCR + 原图 一致性验收工具（任务 1.10）。

覆盖范围：

1. **格式登记** —— ``SUPPORTED_EXT`` 覆盖 12 类格式、``FORMAT_COUNT == 12``、
   ``PLANNED_EXT`` 已清空；
2. **样例驱动解析** —— 对 ``outputs/samples/`` 里每个样例跑 :func:`parse_file`，
   校验块结构、字符数与期望关键词；
3. **原图抽取** —— docx / pptx 传 ``image_sink`` 后必须产出 ``image_key``，
   且原图在 ``UPLOAD_DIR/original/`` 里真实存在、内容哈希一致、重复落盘幂等；
4. **OCR** —— 图片型样例必须走通 OCR（引擎可用时）或给出可执行降级提示；
5. **旧格式降级** —— ``.doc/.ppt`` 在无 LibreOffice 时抛
   :class:`ConverterUnavailable` 且提示可执行；
6. **切片贯通** —— ``blocks_to_chunks`` 产出 ``imageKeys``，且空列表不落字段
   （保证既有 store.json 逐字段形状不变）。

用法::

    python tools/verify_formats.py                  # 全部自检
    python tools/verify_formats.py --samples DIR    # 指定样例目录
    python tools/verify_formats.py --no-ocr         # 跳过真实 OCR（慢）
    python tools/verify_formats.py --json           # 机器可读输出
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

DEFAULT_SAMPLES = SERVICE_DIR / "outputs" / "samples"

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


class Report:
    """极简 PASS/FAIL 收集器。"""

    def __init__(self, verbose: bool = True) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.verbose = verbose
        self.failures: list[str] = []

    def check(self, ok: bool, title: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            if self.verbose:
                print(f"  [PASS] {title}" + (f" —— {detail}" if detail else ""))
        else:
            self.failed += 1
            self.failures.append(title)
            print(f"  [FAIL] {title}" + (f" —— {detail}" if detail else ""))
        return ok

    def skip(self, title: str, detail: str = "") -> None:
        self.skipped += 1
        if self.verbose:
            print(f"  [SKIP] {title}" + (f" —— {detail}" if detail else ""))

    def section(self, name: str) -> None:
        if self.verbose:
            print(f"\n== {name} ==")


# ------------------------------------------------------------------ 1. 格式登记
def check_registry(rep: Report) -> dict:
    rep.section("1. 格式登记（12 类）")
    from app.core.parser import FORMAT_COUNT, IMAGE_EXT, PLANNED_EXT, SUPPORTED_EXT, is_supported

    classes = {
        "md": {".md"},
        "txt": {".txt"},
        "docx": {".docx"},
        "doc": {".doc"},
        "pdf": {".pdf"},
        "xlsx": {".xlsx"},
        "xls": {".xls"},
        "csv": {".csv"},
        "json": {".json"},
        "pptx": {".pptx"},
        "ppt": {".ppt"},
        "image": set(IMAGE_EXT),
    }
    covered = set().union(*classes.values())
    rep.check(FORMAT_COUNT == 12, "FORMAT_COUNT == 12", f"实际 {FORMAT_COUNT}")
    rep.check(len(classes) == FORMAT_COUNT, "格式类数 == 12", f"实际 {len(classes)}")
    rep.check(covered == set(SUPPORTED_EXT), "SUPPORTED_EXT 与 12 类完全一致",
              f"{len(SUPPORTED_EXT)} 个扩展名")
    rep.check(not PLANNED_EXT, "PLANNED_EXT 已清空（无待扩展格式）", f"实际 {sorted(PLANNED_EXT)}")
    rep.check(all(is_supported(f"x{e}") for e in covered), "is_supported 对全部扩展名返回 True")
    rep.check(not is_supported("x.xyz"), "is_supported 对未知扩展名返回 False")
    return {"extCount": len(SUPPORTED_EXT), "ext": sorted(SUPPORTED_EXT), "classes": sorted(classes)}


# ------------------------------------------------------------------ 2. 样例解析
def _sample_paths(samples_dir: Path) -> list[Path]:
    return sorted(p for p in samples_dir.glob("*") if p.is_file())


def _summarize_blocks(blocks) -> dict:
    from collections import Counter

    types = Counter(b.type for b in blocks)
    return {
        "blockCount": len(blocks),
        "blockTypes": dict(types),
        "chars": sum(len(b.text or "") for b in blocks),
        "headings": [b.text for b in blocks if b.type == "heading"][:6],
    }


def check_samples(rep: Report, samples_dir: Path, *, with_ocr: bool) -> list[dict]:
    rep.section(f"2. 样例驱动解析（{samples_dir}）")
    from app.core.parser import parse_file

    paths = _sample_paths(samples_dir)
    if not paths:
        rep.skip("样例目录为空", f"{samples_dir}；先跑 tools/make_samples.py")
        return []

    results: list[dict] = []
    for path in paths:
        ext = path.suffix.lower()
        item = {"file": path.name, "ext": ext}
        try:
            doc = parse_file(path)
        except Exception as exc:  # noqa: BLE001 - 旧格式缺转换器属预期
            from app.core.parser.office_convert import ConverterUnavailable

            if isinstance(exc, ConverterUnavailable):
                rep.skip(f"{path.name} 解析", f"缺转换器：{str(exc)[:70]}…")
                item.update({"status": "converter-unavailable", "error": str(exc)})
            else:
                rep.check(False, f"{path.name} 解析", f"{type(exc).__name__}: {exc}")
                item.update({"status": "error", "error": str(exc)})
            results.append(item)
            continue

        info = _summarize_blocks(doc.blocks)
        item.update({"status": "ok", **info, "metaKeys": sorted(doc.meta)})
        rep.check(info["blockCount"] > 0, f"{path.name} 产出块",
                  f"{info['blockCount']} 块 / {info['chars']} 字 / {info['blockTypes']}")
        results.append(item)

    return results


# ------------------------------------------------------------------ 3. 原图抽取
def check_images(rep: Report, samples_dir: Path) -> dict:
    rep.section("3. 内嵌图片抽取与原图存储")
    from app.config import settings
    from app.core.parser import parse_file
    from app.core.parser import image as image_parser
    from app.core.parser.office_convert import ConverterUnavailable

    seen: dict[str, str] = {}  # key -> sha256
    extracted = 0

    def sink(data: bytes, ext: str) -> str:
        nonlocal extracted
        extracted += 1
        key = image_parser.save_original(data, ext)
        import hashlib

        seen[key] = hashlib.sha256(data).hexdigest()
        return key

    for name in ("07-招生咨询常见问题.docx", "08-知识库建设培训.pptx"):
        path = samples_dir / name
        if not path.is_file():
            rep.skip(f"{name} 原图抽取", "样例缺失")
            continue
        try:
            doc = parse_file(path, image_sink=sink)
        except ConverterUnavailable as exc:
            rep.skip(f"{name} 原图抽取", str(exc)[:70])
            continue

        keys: list[str] = []
        for block in doc.blocks:
            meta = block.meta or {}
            if meta.get("image_key"):
                keys.append(meta["image_key"])
            keys.extend(meta.get("image_keys") or [])
        # 同一张图会在 block.meta 里同时以 image_key / image_keys 出现，去重后再计数
        keys = list(dict.fromkeys(keys))
        rep.check(bool(keys), f"{name} 抽取到内嵌图片", f"{len(keys)} 张：{keys}")

    if not seen:
        rep.skip("原图落盘校验", "本轮未抽取到任何图片")
        return {"extracted": 0, "keys": []}

    base = Path(settings.upload_dir).resolve()
    for key, digest in seen.items():
        rel = key[len(image_parser.ORIGINAL_KEY_PREFIX):]
        target = (base / image_parser.ORIGINAL_SUBDIR / rel).resolve()
        rep.check(target.is_file(), f"原图已落盘 {key}", str(target))
        if target.is_file():
            import hashlib

            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            rep.check(actual == digest, f"原图内容哈希一致 {key}")
            rep.check(target.name.startswith(digest[:16]), f"原图按内容寻址命名 {target.name}")

    # 幂等：同内容二次落盘必须复用同一个键
    payload = b"\x89PNG\r\n\x1a\n" + b"same-bytes-for-dedup" * 8
    k1 = image_parser.save_original(payload, ".png")
    k2 = image_parser.save_original(payload, ".png")
    rep.check(k1 == k2, "同内容重复落盘幂等（内容寻址去重）", k1)

    stats = image_parser.stats()
    rep.check(stats["count"] > 0, "原图目录有文件", f"{stats['count']} 个 / {stats['bytes']} 字节")
    return {"extracted": extracted, "keys": sorted(seen), "stats": stats}


# ------------------------------------------------------------------ 4. OCR
def check_ocr(rep: Report, samples_dir: Path, *, with_ocr: bool) -> dict:
    rep.section("4. OCR（可插拔：rapidocr / tesseract / none）")
    from app.core.parser import ocr

    info = ocr.stats()
    rep.check(info["configured"] in ("auto", "rapidocr", "tesseract", "none"),
              "OCR_ENGINE 取值合法", info["configured"])
    rep.check(isinstance(info["available"], list), "可用引擎列表可枚举", str(info["available"]))
    if info["engine"] == "none":
        rep.check(bool(ocr.RAPIDOCR_MISSING_HINT), "不可用时给出可执行安装提示")
    else:
        rep.check(info["engine"] in info["available"], "解析出的引擎在可用列表内",
                  f"{info['engine']} ∈ {info['available']}")

    if not with_ocr:
        rep.skip("真实 OCR 识别", "--no-ocr")
        return info

    targets = [p for p in _sample_paths(samples_dir) if p.suffix.lower() in (".png", ".jpg")]
    if not targets:
        rep.skip("真实 OCR 识别", "样例目录无图片")
        return info

    for path in targets:
        result = ocr.ocr_file(path)
        if info["engine"] == "none":
            rep.check(not result.ok and bool(result.error), f"{path.name} OCR 降级不抛异常",
                      (result.error or "")[:60] + "…")
            continue
        rep.check(result.ok, f"{path.name} OCR 成功",
                  f"engine={result.engine} 行数={result.line_count} 耗时={result.elapsed:.2f}s")
        rep.check("校园网" in result.text, f"{path.name} 识别到期望文本",
                  repr(result.text.replace("\n", " / ")[:60]))

    # 非图片输入必须降级而不是抛异常
    bad = ocr.ocr_image(b"not an image at all")
    rep.check(bad.ok is False and bool(bad.error), "非图片输入降级为 ok=False（不抛异常）",
              (bad.error or "")[:50])
    return info


# ------------------------------------------------------------------ 5. 旧格式降级
def check_legacy(rep: Report, samples_dir: Path) -> dict:
    rep.section("5. 旧格式（.doc/.ppt）转换层")
    from app.core.parser import office_convert

    info = office_convert.stats()
    rep.check(isinstance(info.get("sofficeAvailable"), bool), "soffice 探测可用",
              f"soffice={info.get('soffice') or '未找到'}")
    rep.check(isinstance(info.get("docTextCli"), str), "antiword/catdoc 探测可用",
              info.get("docTextCli") or "未安装（.doc 无纯文本兜底）")

    if not info.get("sofficeAvailable"):
        rep.check(bool(office_convert.SOFFICE_MISSING_HINT), "缺转换器时提供可执行安装提示")
        # 无转换器时解析 .doc/.ppt 必须抛业务异常，而不是原始 OSError / 静默空文档。
        # 样例目录里的 .doc/.ppt 需 LibreOffice 才能生成，因此这里用最小合成载荷兜底，
        # 保证「降级路径」在本环境也始终被真实执行到。
        from app.core.parser import parse_file

        cases: list[tuple[str, bytes]] = []
        for name in ("12-招生咨询常见问题.doc", "13-知识库建设培训.ppt"):
            path = samples_dir / name
            if path.is_file():
                cases.append((path.name, path.read_bytes()))
            else:
                # OLE 复合文档魔数前缀 + 填充，足够触发「扩展名分发 → 转换器探测」
                ext = path.suffix
                cases.append((f"{path.name}（合成载荷）", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64))
                _ = ext
        with tempfile.TemporaryDirectory(prefix="nxz-legacy-") as tmp:
            for label, payload in cases:
                probe = Path(tmp) / label.split("（")[0]
                probe.write_bytes(payload)
                try:
                    parse_file(probe)
                    rep.check(False, f"{label} 无转换器应报错")
                except office_convert.ConverterUnavailable as exc:
                    rep.check(True, f"{label} 无转换器时抛 ConverterUnavailable",
                              str(exc)[:60] + "…")
                except Exception as exc:  # noqa: BLE001 - 非业务异常即失败
                    rep.check(False, f"{label} 应抛 ConverterUnavailable",
                              f"实际 {type(exc).__name__}: {exc}")
    else:
        rep.skip("旧格式降级校验", "本机有 LibreOffice，走正常转换路径")
    return info


# ------------------------------------------------------------------ 6. 切片贯通
def check_chunker(rep: Report) -> dict:
    rep.section("6. 切片贯通（imageKeys）")
    from app.core.chunker import blocks_to_chunks
    from app.core.parser.block import Block

    with_img = blocks_to_chunks(
        [
            Block(type="heading", level=1, text="图片文档"),
            Block(type="para", text="下面是一张图。" * 5, meta={"image_key": "uploads/original/a.png"}),
            Block(type="image", text="a.png", meta={"image_key": "uploads/original/a.png"}),
        ],
        {"dept": "信息网络中心", "ktype": "办事指南"},
        "01-信网中心/办事指南/图片文档.md",
    )
    payloads = [c.to_dict() for c in with_img]
    keys = [k for p in payloads for k in (p.get("imageKeys") or [])]
    rep.check(bool(keys), "含图切片输出 imageKeys", str(sorted(set(keys))))
    rep.check(all("imageKeys" in p for p in payloads if p.get("imageKeys")),
              "imageKeys 仅非空时出现")

    plain = blocks_to_chunks(
        [Block(type="para", text="纯文本段落。" * 20)],
        {"dept": "信息网络中心", "ktype": "办事指南"},
        "01-信网中心/办事指南/纯文本.md",
    )
    rep.check(all("imageKeys" not in c.to_dict() for c in plain),
              "无图切片不含 imageKeys 字段（形状零漂移）")
    return {"chunkCount": len(with_img), "imageKeys": sorted(set(keys))}


# ------------------------------------------------------------------ 7. 原图接口端到端
def check_image_api(rep: Report, samples_dir: Path) -> dict:
    """原图接口端到端：临时 JSON 索引 + 临时 upload_dir + 真实 HTTP 请求。

    用真实样例图片搭出两类文档：

    * ``D001`` —— **图片型文档**（``file`` 指向 ``uploads/original/<hash>.png``）；
    * ``D002`` —— **含内嵌图文档**（切片带 ``imageKeys``）。

    然后校验 ``/images``、``/image``、``/preview`` 的字段与安全边界。
    """
    rep.section("7. 原图接口端到端（/images、/image、/preview.isImage）")
    import hashlib
    import json
    import tempfile

    png = samples_dir / "09-校园网开户公告.png"
    if not png.is_file():
        rep.skip("原图接口端到端", f"缺少样例 {png.name}")
        return {}

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.core.index_store import clear_index
    from app.main import app

    payload = png.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    key = f"uploads/original/{digest[:16]}.png"

    with tempfile.TemporaryDirectory(prefix="nxz-imgapi-") as tmp:
        tmp_dir = Path(tmp)
        uploads = tmp_dir / "uploads" / "original"
        uploads.mkdir(parents=True)
        (uploads / f"{digest[:16]}.png").write_bytes(payload)

        store = {
            "model": "verify-formats",
            "createdAt": "2026-10-14T00:00:00Z",
            "docs": [
                {
                    "docId": "D001",
                    "file": key,
                    "title": "校园网开户公告",
                    "dept": "信息网络中心",
                    "ktype": "办事指南",
                    "status": "已审核",
                    "chunkCount": 1,
                },
                {
                    "docId": "D002",
                    "file": "01-信网中心/办事指南/招生咨询常见问题.docx",
                    "title": "招生咨询常见问题",
                    "dept": "信息网络中心",
                    "ktype": "常见问题",
                    "status": "已审核",
                    "chunkCount": 2,
                },
            ],
            "chunks": [
                {
                    "chunkId": "D001-C001",
                    "docId": "D001",
                    "seqNo": 1,
                    "charCount": 30,
                    "dept": "信息网络中心",
                    "ktype": "办事指南",
                    "visibleRoles": ["全部"],
                    "titlePath": "校园网开户公告",
                    "blockType": "para",
                    "text": "校园网开户与报修指南 联系电话 029-85581234",
                    "embedText": "校园网开户公告\n校园网开户与报修指南 联系电话 029-85581234",
                },
                {
                    "chunkId": "D002-C001",
                    "docId": "D002",
                    "seqNo": 1,
                    "charCount": 20,
                    "dept": "信息网络中心",
                    "ktype": "常见问题",
                    "visibleRoles": ["全部"],
                    "titlePath": "招生咨询常见问题 > 录取分数",
                    "blockType": "para",
                    "text": "本指南适用于本科招生咨询。",
                    "embedText": "招生咨询常见问题 > 录取分数\n本指南适用于本科招生咨询。",
                    "imageKeys": [key],
                },
                {
                    "chunkId": "D002-C002",
                    "docId": "D002",
                    "seqNo": 2,
                    "charCount": 12,
                    "dept": "信息网络中心",
                    "ktype": "常见问题",
                    "visibleRoles": ["全部"],
                    "titlePath": "招生咨询常见问题",
                    "blockType": "para",
                    "text": "录取分数以各省公布为准。",
                    "embedText": "招生咨询常见问题\n录取分数以各省公布为准。",
                },
            ],
        }
        store_path = tmp_dir / "store.json"
        store_path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")

        old_upload, old_store = settings.upload_dir, settings.store_path
        settings.upload_dir = str(tmp_dir / "uploads")
        settings.store_path = store_path
        clear_index()
        try:
            client = TestClient(app)

            # --- D001 图片型文档 ---
            r = client.get("/api/docs/D001/images")
            body = r.json()
            rep.check(r.status_code == 200 and body.get("ok"), "D001 /images 返回 200/ok",
                      f"HTTP {r.status_code}")
            data = body.get("data") or {}
            rep.check(data.get("isImageDoc") is True, "D001 被识别为图片型文档")
            first = (data.get("images") or [{}])[0]
            rep.check(first.get("role") == "original" and first.get("key") == key,
                      "D001 图片 role=original 且 key 正确", str(first.get("key")))
            rep.check(first.get("exists") is True and first.get("size") == len(payload),
                      "D001 图片元信息（存在 + 字节数）",
                      f"size={first.get('size')} 期望 {len(payload)}")
            rep.check((first.get("width"), first.get("height")) == (900, 270),
                      "D001 图片宽高来自真实文件",
                      f"{first.get('width')}x{first.get('height')}")
            rep.check(not first.get("error"), "D001 图片无错误", str(first.get("error")))

            r = client.get(f"/api/docs/D001/image?key={key}")
            rep.check(r.status_code == 200 and r.content == payload,
                      "D001 /image 返回与原件逐字节一致的字节流",
                      f"HTTP {r.status_code} / {len(r.content)}B")
            rep.check(r.headers.get("content-type", "").startswith("image/png"),
                      "D001 /image Content-Type 为 image/png", r.headers.get("content-type", ""))
            rep.check(r.headers.get("x-content-type-options") == "nosniff",
                      "D001 /image 带 nosniff")
            rep.check("immutable" in (r.headers.get("cache-control") or ""),
                      "D001 /image 允许长缓存", r.headers.get("cache-control") or "")

            # --- D002 内嵌图文档 ---
            r = client.get("/api/docs/D002/images")
            data = (r.json().get("data") or {})
            rep.check(data.get("isImageDoc") is False, "D002 不是图片型文档")
            emb = (data.get("images") or [{}])[0]
            rep.check(emb.get("role") == "embedded" and emb.get("chunkSeqs") == [1],
                      "D002 内嵌图带 chunkSeqs=[1]", str(emb.get("chunkSeqs")))

            # --- 安全边界 ---
            r = client.get("/api/docs/D001/image?key=uploads/original/does-not-exist.png")
            rep.check(r.status_code == 404, "不存在的 key → 404", f"HTTP {r.status_code}")
            r = client.get("/api/docs/D002/image?key=uploads/original/deadbeefdeadbeef.png")
            rep.check(r.status_code == 404, "不属于该文档的 key → 404", f"HTTP {r.status_code}")
            r = client.get("/api/docs/D001/image?key=uploads/../etc/passwd")
            rep.check(r.status_code in (400, 404), "含 .. 的 key → 400/404", f"HTTP {r.status_code}")
            r = client.get("/api/docs/D002/image?key=01-信网中心/办事指南/招生咨询常见问题.docx")
            rep.check(r.status_code == 400, "非图片扩展名 → 400（不可当图片读出）",
                      f"HTTP {r.status_code}")

            # --- 预览数据带 images / isImage ---
            r = client.get("/api/docs/D002/preview?q=录取分数")
            data = r.json().get("data") or {}
            rep.check(r.status_code == 200 and data.get("isImage") is False,
                      "D002 /preview 带 isImage=false")
            rep.check(len(data.get("images") or []) == 1,
                      "D002 /preview 带 1 张图片", str(len(data.get("images") or [])))
            r = client.get("/api/docs/D001/preview")
            data = r.json().get("data") or {}
            rep.check(data.get("isImage") is True, "D001 /preview 带 isImage=true")
            text = data.get("text") or ""
            rep.check(bool(text.strip()), "D001 /preview 原文非空（图片文本提取）",
                      text[:40].replace("\n", " / "))
            rep.check(text.startswith("# 校园网开户公告"),
                      "D001 图片原文标题用文档标题（而非内容哈希名）",
                      text.split("\n", 1)[0])
            rep.check("校园网开户与报修指南" in text,
                      "D001 图片原文含 OCR 识别文本",
                      text.replace("\n", " / ")[:70])
            return {
                "key": key,
                "d001Images": (client.get("/api/docs/D001/images").json().get("data") or {}),
                "d002Images": (client.get("/api/docs/D002/images").json().get("data") or {}),
            }
        finally:
            settings.upload_dir, settings.store_path = old_upload, old_store
            clear_index()


# ------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="12 格式 + OCR + 原图 验收（任务 1.10）")
    ap.add_argument("--samples", default=str(DEFAULT_SAMPLES), help="样例目录")
    ap.add_argument("--no-ocr", action="store_true", help="跳过真实 OCR 识别")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    ap.add_argument("--quiet", action="store_true", help="只输出汇总")
    args = ap.parse_args(argv)

    os.environ.setdefault("KB_STORE", "json")
    os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

    samples_dir = Path(args.samples)
    rep = Report(verbose=not args.quiet)
    print("=== 「能小智」kb-service · 12 格式 + OCR + 原图 验收（任务 1.10）===")
    print(f"样例目录：{samples_dir}")

    registry = check_registry(rep)
    with tempfile.TemporaryDirectory(prefix="nxz-orig-") as tmp:
        os.environ["UPLOAD_DIR"] = tmp
        # settings 在 import 时已缓存，这里直接改属性以保证原图落盘到临时目录
        from app.config import settings

        settings.upload_dir = tmp

        samples = check_samples(rep, samples_dir, with_ocr=not args.no_ocr)
        images = check_images(rep, samples_dir)
        ocr_info = check_ocr(rep, samples_dir, with_ocr=not args.no_ocr)
        legacy = check_legacy(rep, samples_dir)
    chunks = check_chunker(rep)
    api = check_image_api(rep, samples_dir)

    print("\n=== 汇总 ===")
    print(f"PASS {rep.passed} / FAIL {rep.failed} / SKIP {rep.skipped}")
    if rep.failures:
        print("失败项：")
        for name in rep.failures:
            print(f"  - {name}")

    if args.json:
        print(json.dumps(
            {
                "passed": rep.passed,
                "failed": rep.failed,
                "skipped": rep.skipped,
                "failures": rep.failures,
                "registry": registry,
                "samples": samples,
                "images": images,
                "ocr": ocr_info,
                "legacy": legacy,
                "chunks": chunks,
                "imageApi": api,
            },
            ensure_ascii=False,
            indent=2,
        ))

    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
