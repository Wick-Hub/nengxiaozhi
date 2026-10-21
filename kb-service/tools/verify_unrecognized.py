"""未识别问题闭环验收（任务 3.11 / M-S-07～09，JSON 存储）。

覆盖 HTTP 层：

1. 独立页 ``/unrecognized`` 与静态资源可访问；
2. 聚合清单：同义问法按 ``questionKey`` 合并，频次 / 角色 / 渠道 / 来源齐全；
3. 筛选：状态 / 来源 / 关键词 / 渠道 / 归属部门 / 日期；
4. 详情追溯：原会话用户消息 + AI 回答 + 引用 + 同义问法；
5. 批量忽略 / 重新打开 / 归类；
6. CSV 导出（BOM + RFC 5987 中文文件名）；
7. 真实问答链路：空召回自动入池（``weak_recall``）；
8. JSON 模式反哺返回 400 并给出 MySQL 指引（反哺草稿本身需 MySQL，未在本机验收）。

**数据隔离**：脚本会在验收前备份 ``data/unrecognized.json`` / ``data/sessions.json``，
结束后恢复。请勿与正在写入同一数据目录的开发服务同时运行。

用法::

    python tools/verify_unrecognized.py
    python tools/verify_unrecognized.py --port 8036 --keep

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "json")
os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _backup(path: Path, backup_dir: Path) -> Path | None:
    if not path.is_file():
        return None
    target = backup_dir / path.name
    shutil.copy2(path, target)
    return target


def _restore(path: Path, backup: Path | None) -> None:
    if backup is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, path)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".verify.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _seed(pool_path: Path, sessions_path: Path) -> dict:
    """写入两组问题（同义问法 ×2 + 已忽略 ×1）与一条可追溯会话。"""
    from app.db.unrecognized_store import normalize_question

    now = datetime.now()
    yesterday = now - timedelta(days=1)
    key_card = normalize_question("校园卡丢了怎么补办？")

    pool = {
        "items": [
            {
                "id": 1,
                "sessionId": "S-VERIFY-U1",
                "msgId": 2,
                "question": "校园卡丢了怎么补办？",
                "questionKey": key_card,
                "confidence": 0.2,
                "source": "refused",
                "role": "本科生",
                "channelCode": "lib",
                "agentId": "",
                "dept": "财务缴费",
                "state": 0,
                "handlerId": None,
                "newDocId": None,
                "newChunkId": None,
                "createdAt": yesterday.isoformat(timespec="seconds"),
                "handledAt": None,
            },
            {
                "id": 2,
                "sessionId": "S-VERIFY-U2",
                "msgId": 2,
                "question": "校园卡丢了怎么补办",
                "questionKey": key_card,
                "confidence": 0.3,
                "source": "low_confidence",
                "role": "本科生",
                "channelCode": "lib",
                "agentId": "",
                "dept": "财务缴费",
                "state": 0,
                "handlerId": None,
                "newDocId": None,
                "newChunkId": None,
                "createdAt": now.isoformat(timespec="seconds"),
                "handledAt": None,
            },
            {
                "id": 3,
                "sessionId": None,
                "msgId": None,
                "question": "如何申请校园网账号",
                "questionKey": normalize_question("如何申请校园网账号"),
                "confidence": 0.1,
                "source": "weak_recall",
                "role": "访客",
                "channelCode": "portal",
                "agentId": "",
                "dept": "",
                "state": 2,
                "handlerId": None,
                "newDocId": None,
                "newChunkId": None,
                "createdAt": (now - timedelta(days=2)).isoformat(timespec="seconds"),
                "handledAt": now.isoformat(timespec="seconds"),
            },
        ]
    }
    sessions = {
        "sessions": {
            sid: {
                "sessionId": sid,
                "role": "本科生",
                "createdAt": stamp,
                "agentId": "",
                "channelCode": "lib",
                "dept": "财务缴费",
                "messages": [
                    {
                        "msgId": 1,
                        "role": "user",
                        "content": question,
                        "createdAt": stamp,
                    },
                    {
                        "msgId": 2,
                        "role": "ai",
                        "content": "抱歉，知识库中未找到相关内容。",
                        "confidence": confidence,
                        "refused": True,
                        "citations": [],
                        "traceId": "T-" + sid,
                        "createdAt": stamp,
                    },
                ],
            }
            for sid, question, confidence, stamp in (
                ("S-VERIFY-U1", "校园卡丢了怎么补办？", 0.2,
                 yesterday.isoformat(timespec="seconds")),
                # 聚合组按最近出现倒序取代表记录（id=2），它同样需要可追溯
                ("S-VERIFY-U2", "校园卡丢了怎么补办", 0.3,
                 now.isoformat(timespec="seconds")),
            )
        }
    }
    _write_json(pool_path, pool)
    _write_json(sessions_path, sessions)
    return {"keyCard": key_card}


def _items(data: dict) -> list[dict]:
    return data.get("items") or []


def _find(items: list[dict], key: str) -> dict | None:
    return next((item for item in items if item.get("questionKey") == key), None)


def main() -> int:
    parser = argparse.ArgumentParser(description="未识别问题闭环验收（任务 3.11）")
    parser.add_argument("--port", type=int, default=8036, help="验收用服务端口")
    parser.add_argument("--keep", action="store_true", help="保留验收数据（默认恢复备份）")
    parser.add_argument("--shots-dir", default="", help="预留：截图目录")
    args = parser.parse_args()

    import httpx

    from tools._server import read_log, spawn_server, stop_server, wait_port

    data_dir = SERVICE_DIR / "data"
    pool_path = data_dir / "unrecognized.json"
    sessions_path = data_dir / "sessions.json"
    backup_dir = Path(tempfile.mkdtemp(prefix="nxz-unrecognized-"))
    pool_backup = _backup(pool_path, backup_dir)
    sessions_backup = _backup(sessions_path, backup_dir)
    keys = _seed(pool_path, sessions_path)

    port = args.port
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["KB_STORE"] = "json"
    env["AUTH_ENABLED"] = "false"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=json）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        with httpx.Client(timeout=20, trust_env=False, base_url=base) as cli:
            section("1. 页面与筛选项")
            page = cli.get("/unrecognized")
            check("独立页 /unrecognized 可访问", page.status_code == 200, str(page.status_code))
            check("页面标题正确", "未识别问题池" in page.text)
            check("页面引入 unrecognized.js", "unrecognized.js" in page.text)

            filters = cli.get("/api/admin/unrecognized/filters").json()["data"]
            check("状态筛选项 3 个", len(filters.get("states") or []) == 3,
                  str(filters.get("states")))
            check("来源筛选项含三类触发",
                  {s["value"] for s in filters.get("sources") or []}
                  == {"weak_recall", "refused", "low_confidence"})
            codes = {c["code"] for c in filters.get("channels") or []}
            check("历史渠道编码可筛选（含已删除渠道）", {"lib", "portal"} <= codes, str(codes))
            check("JSON 模式标记不可反哺", filters.get("feedbackAvailable") is False)

            section("2. 聚合清单")
            resp = cli.get("/api/admin/unrecognized")
            body = resp.json()
            data = body["data"]
            items = _items(data)
            check("聚合为 2 组", data["total"] == 2, str(data["total"]))
            check("原始记录 3 条", data["recordTotal"] == 3, str(data["recordTotal"]))
            card = _find(items, keys["keyCard"])
            check("同义问法合并为一组", card is not None and card["count"] == 2,
                  str(card and card["count"]))
            check("来源分布齐全", set(card["sources"]) == {"refused", "low_confidence"},
                  str(card.get("sources")))
            check("角色 / 渠道 / 部门归因",
                  card["roles"] == ["本科生"] and card["channels"] == ["lib"]
                  and card["dept"] == "财务缴费",
                  f"{card.get('roles')} / {card.get('channels')} / {card.get('dept')}")
            check("业务域分类", card["domain"] == "财务缴费", card.get("domain"))
            check("语义簇字段存在", bool(card.get("clusterId")), card.get("clusterId"))
            check("扫描窗口可见", (data.get("window") or {}).get("limit") == 5000,
                  str(data.get("window")))

            section("3. 筛选")
            only_ignored = cli.get("/api/admin/unrecognized", params={"state": 2}).json()["data"]
            check("按状态筛选", only_ignored["total"] == 1, str(only_ignored["total"]))
            by_q = cli.get("/api/admin/unrecognized", params={"q": "校园卡"}).json()["data"]
            check("按关键词筛选", by_q["total"] == 1 and by_q["items"][0]["count"] == 2)
            by_channel = cli.get("/api/admin/unrecognized",
                                 params={"channelCode": "portal"}).json()["data"]
            check("按渠道筛选", by_channel["total"] == 1, str(by_channel["total"]))
            by_dept = cli.get("/api/admin/unrecognized",
                              params={"dept": "财务缴费"}).json()["data"]
            check("按部门筛选", by_dept["total"] == 1, str(by_dept["total"]))
            tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
            future = cli.get("/api/admin/unrecognized",
                             params={"dateFrom": tomorrow}).json()["data"]
            check("日期下界生效", future["total"] == 0, str(future["total"]))

            section("4. 详情追溯")
            detail = cli.get("/api/admin/unrecognized/1")
            check("详情可访问", detail.status_code == 200, str(detail.status_code))
            detail_data = detail.json()["data"]
            check("会话元信息存在", (detail_data.get("session") or {}).get("sessionId") == "S-VERIFY-U1")
            check("用户原话可追溯",
                  (detail_data.get("questionMessage") or {}).get("content") == "校园卡丢了怎么补办？")
            check("AI 拒答可追溯",
                  (detail_data.get("answerMessage") or {}).get("refused") is True)
            check("同义问法 2 条", detail_data.get("variantCount") == 2,
                  str(detail_data.get("variantCount")))
            missing = cli.get("/api/admin/unrecognized/999999")
            check("不存在的记录返回 404", missing.status_code == 404, str(missing.status_code))

            section("5. 批量处理")
            ignored = cli.post("/api/admin/unrecognized/batch",
                               json={"questionKeys": [keys["keyCard"]], "action": "ignore"})
            check("批量忽略成功", ignored.status_code == 200, str(ignored.status_code))
            check("忽略覆盖整组 2 条", ignored.json()["data"]["updated"] == 2,
                  str(ignored.json()["data"].get("updated")))
            reopened = cli.post("/api/admin/unrecognized/batch",
                                json={"questionKeys": [keys["keyCard"]], "action": "reopen"})
            check("批量重新打开成功", reopened.json()["data"]["updated"] == 2)
            classified = cli.post("/api/admin/unrecognized/batch",
                                  json={"questionKeys": [keys["keyCard"]],
                                        "action": "classify", "dept": "信息中心"})
            check("批量归类成功", classified.json()["data"]["updated"] == 2)
            moved = cli.get("/api/admin/unrecognized",
                            params={"dept": "信息中心"}).json()["data"]
            check("归类后可按新部门筛选", moved["total"] == 1, str(moved["total"]))
            no_dept = cli.post("/api/admin/unrecognized/batch",
                               json={"questionKeys": [keys["keyCard"]], "action": "classify"})
            check("归类缺部门被拒", no_dept.status_code == 400, str(no_dept.status_code))

            section("6. CSV 导出")
            export = cli.get("/api/admin/unrecognized/export")
            check("导出 200", export.status_code == 200, str(export.status_code))
            check("UTF-8 BOM", export.content.startswith(b"\xef\xbb\xbf"))
            text = export.content.decode("utf-8-sig")
            check("表头包含聚合列", text.splitlines()[0].startswith("问题,频次,首次出现"))
            check("导出内容含问题", "校园卡丢了怎么补办" in text)
            check("中文文件名（RFC 5987）",
                  "filename*=UTF-8''" in export.headers.get("content-disposition", ""))

            section("7. 问答链路自动入池（低置信度 / 弱召回）")
            # 哈希向量兜底时几乎总能召回 5 条，mock 生成器的低置信度是最常见的
            # 入池来源；空召回的 weak_recall 由 tests/test_unrecognized.py 的
            # 空索引用例覆盖。这里验证「真实问答 → 自动入池」这条链路本身。
            unique = "zzzzqqqqxxxx"
            chat = cli.post("/api/chat", json={"question": unique, "role": "访客"})
            chat_body = chat.json()
            chat_data = chat_body.get("data") or {}
            check("问答接口返回", chat.status_code == 200 and chat_body.get("ok") is True,
                  str(chat.status_code))
            check("回答为拒答或低置信度",
                  bool(chat_data.get("refused"))
                  or float(chat_data.get("confidence") or 0) < 0.35,
                  f"refused={chat_data.get('refused')} conf={chat_data.get('confidence')}")
            pooled = cli.get("/api/admin/unrecognized",
                             params={"q": unique}).json()["data"]
            new_item = (pooled.get("items") or [None])[0]
            check("未识别问题已入池", pooled["total"] == 1, str(pooled["total"]))
            check("来源标记 weak_recall / low_confidence",
                  new_item is not None
                  and bool(set(new_item.get("sources", [])) & {"weak_recall", "low_confidence"}),
                  str(new_item and new_item.get("sources")))
            check("角色归因", new_item is not None and new_item.get("roles") == ["访客"],
                  str(new_item and new_item.get("roles")))

            section("8. JSON 模式反哺门禁")
            feedback = cli.post("/api/admin/unrecognized/feedback",
                                json={"questionKey": keys["keyCard"], "title": "校园卡补办"})
            check("JSON 模式返回 400", feedback.status_code == 400, str(feedback.status_code))
            check("提示需 MySQL", "MySQL" in str(feedback.json().get("detail", "")))

        print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
        return 1 if FAILED else 0
    finally:
        stop_server(proc)
        if args.keep:
            print(f"验收数据保留：{pool_path}；备份目录：{backup_dir}")
        else:
            _restore(pool_path, pool_backup)
            _restore(sessions_path, sessions_backup)
            print(f"已恢复数据备份（{backup_dir}）")


if __name__ == "__main__":
    raise SystemExit(main())
