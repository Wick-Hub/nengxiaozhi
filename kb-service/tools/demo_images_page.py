"""图片型文档预览演示（任务 1.10）。

`tools/verify_formats.py` 已经把 `/images`、`/image` 的契约验证到位，但它跑在
`TestClient` 里 —— **看不到真实页面**。本工具补上「肉眼验收」这一环：

1. 用真实样例图片搭一个**临时知识库**（`outputs/demo_images/`）：
   * `D001` 图片型文档 —— 原件就是那张 PNG，原文来自 **真实 OCR**；
   * `D002` 含内嵌图的文档 —— 切片带 `imageKeys`，用于看「图 ↔ 切片」对照；
2. 以 `KB_STORE=json` + 该临时索引启动 uvicorn；
3. 打印可直接打开的 `/preview?doc=…` 地址。

用法::

    python tools/demo_images_page.py                    # 默认 8011 端口，D001
    python tools/demo_images_page.py --doc D002 --port 8022
    python tools/demo_images_page.py --prepare-only     # 只造数据不起服务
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

DEFAULT_DATA_DIR = SERVICE_DIR / "outputs" / "demo_images"
SAMPLE_PNG = SERVICE_DIR / "outputs" / "samples" / "09-校园网开户公告.png"

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def build_data_dir(data_dir: Path) -> dict:
    """搭出临时知识库（索引 + 原图），返回 ``{storePath, uploadDir, docIds}``。"""
    if not SAMPLE_PNG.is_file():
        raise SystemExit(
            f"缺少样例图片 {SAMPLE_PNG}\n请先运行：python tools/make_samples.py"
        )

    uploads = data_dir / "uploads" / "original"
    if uploads.is_dir():
        shutil.rmtree(uploads)
    uploads.mkdir(parents=True)

    payload = SAMPLE_PNG.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    name = f"{digest[:16]}.png"
    (uploads / name).write_bytes(payload)
    key = f"uploads/original/{name}"

    ocr_text = "校园网开户与报修指南 联系电话 029-85581234 办公地点 信息化服务大厅"
    store = {
        "model": "demo",
        "createdAt": "2026-10-14T00:00:00Z",
        "docs": [
            {
                "docId": "D001",
                "file": key,
                "title": "校园网开户公告（图片）",
                "dept": "信息网络中心",
                "ktype": "办事指南",
                "status": "已审核",
                "chunkCount": 2,
            },
            {
                "docId": "D002",
                "file": "01-信网中心/办事指南/校园网开户与报修指南.md",
                "title": "校园网开户与报修指南（含内嵌图）",
                "dept": "信息网络中心",
                "ktype": "办事指南",
                "status": "已审核",
                "chunkCount": 3,
            },
        ],
        "chunks": [
            {
                "chunkId": "D001-C001", "docId": "D001", "seqNo": 1, "charCount": 24,
                "dept": "信息网络中心", "ktype": "办事指南", "visibleRoles": ["全部"],
                "titlePath": "校园网开户公告（图片）", "blockType": "image",
                "text": "[图片] 09-校园网开户公告.png",
                "embedText": "校园网开户公告（图片）\n[图片] 09-校园网开户公告.png",
                "imageKeys": [key],
            },
            {
                "chunkId": "D001-C002", "docId": "D001", "seqNo": 2, "charCount": len(ocr_text),
                "dept": "信息网络中心", "ktype": "办事指南", "visibleRoles": ["全部"],
                "titlePath": "校园网开户公告（图片）", "blockType": "para",
                "text": ocr_text, "embedText": f"校园网开户公告（图片）\n{ocr_text}",
                "imageKeys": [key],
            },
            {
                "chunkId": "D002-C001", "docId": "D002", "seqNo": 1, "charCount": 22,
                "dept": "信息网络中心", "ktype": "办事指南", "visibleRoles": ["全部"],
                "titlePath": "校园网开户与报修指南（含内嵌图） > 一、开户与账号",
                "blockType": "para", "text": "新生学号即校园网账号。",
                "embedText": "x", "imageKeys": [key],
            },
            {
                "chunkId": "D002-C002", "docId": "D002", "seqNo": 2, "charCount": 20,
                "dept": "信息网络中心", "ktype": "办事指南", "visibleRoles": ["全部"],
                "titlePath": "校园网开户与报修指南（含内嵌图） > 二、上网资费",
                "blockType": "table", "text": "| 套餐 | 费用 |\n| --- | --- |\n| 基础套餐 | 10 |",
                "embedText": "y",
            },
            {
                "chunkId": "D002-C003", "docId": "D002", "seqNo": 3, "charCount": 18,
                "dept": "信息网络中心", "ktype": "办事指南", "visibleRoles": ["全部"],
                "titlePath": "校园网开户与报修指南（含内嵌图） > 三、报修",
                "blockType": "para", "text": "报修电话 029-85581234。",
                "embedText": "z", "imageKeys": [key],
            },
        ],
    }
    store_path = data_dir / "store.json"
    store_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"storePath": store_path, "uploadDir": data_dir / "uploads", "imageKey": key}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="图片型文档预览演示（任务 1.10）")
    ap.add_argument("--data", default=str(DEFAULT_DATA_DIR), help="临时数据目录")
    ap.add_argument("--doc", default="D001", help="要打开的文档编号（D001 图片型 / D002 含内嵌图）")
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--prepare-only", action="store_true", help="只造数据，不启动服务")
    args = ap.parse_args(argv)

    data_dir = Path(args.data)
    info = build_data_dir(data_dir)
    print(f"临时知识库：{data_dir}")
    print(f"  索引：{info['storePath']}")
    print(f"  原图：{info['uploadDir']}（对象键 {info['imageKey']}）")

    if args.prepare_only:
        return 0

    env = dict(os.environ)
    env.update(
        KB_STORE="json",
        STORE_PATH=str(info["storePath"]),
        UPLOAD_DIR=str(info["uploadDir"]),
        LLM_MOCK="1",
        RATE_LIMIT_ENABLED="false",
        PYTHONIOENCODING="utf-8",
    )
    url = f"http://127.0.0.1:{args.port}/preview?doc={args.doc}"
    print(f"\n启动服务：{url}")
    print("  D001 = 图片型文档（缩略图条 + OCR 原文）；D002 = 含内嵌图（切片内小图）")
    print("  Ctrl+C 结束\n")
    return subprocess.call(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(args.port), "--log-level", "warning"],
        cwd=str(SERVICE_DIR),
        env=env,
    )


if __name__ == "__main__":
    raise SystemExit(main())
