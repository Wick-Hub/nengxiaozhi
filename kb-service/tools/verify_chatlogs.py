"""会话日志管理验收（任务 2.1 / 关联 M-S-01）。

六组自检，**默认不依赖 MySQL**（``KB_STORE=json`` 读 ``data/sessions.json``），
可直接复现；带 ``--store mysql`` 可在有库环境跑同一套断言。

1. **服务层·问答视图**：结构完整、总数、排序（时间倒序）、分页不重不漏；
2. **筛选与检索**：角色筛选、关键词命中「问题」与「回答」、时间范围、组合筛选；
3. **咨询用户视图**：会话聚合一致（问数之和 == 问答总数）、角色与末次时间；
4. **CSV 导出**：表头列名、行数、UTF-8 BOM、「导出与页面同一筛选口径」；
5. **接口层契约**：真实起服务跑 ``/chatlogs`` / ``/users`` / ``/filters`` / ``/export``
   与参数校验（``pageSize`` 越界 → 422）；
6. **鉴权**：``AUTH_ENABLED=true`` 的独立实例上，无 token → 401，非管理员 token → 403。

用法::

    python tools/verify_chatlogs.py
    python tools/verify_chatlogs.py --store mysql          # 有库时验证 MySQL 分支
    python tools/verify_chatlogs.py --port 8021

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

# 必须在导入 app.* 之前定型（settings 在导入期读取环境变量）
os.environ.setdefault("KB_STORE", "json")

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


# ==================================================================== 1. 服务层
def check_qa_view(store: str) -> dict:
    from app.services import chatlog_admin

    section("1. 服务层 · 问答视图")
    page1 = chatlog_admin.query_qa(page=1, page_size=10)
    check("返回结构含 items/total/page/pageSize/totalPages",
          all(k in page1 for k in ("items", "total", "page", "pageSize", "totalPages")),
          f"keys={sorted(page1)[:4]}…")
    check("store 标识与运行模式一致", page1.get("store") == store, f"store={page1.get('store')}")
    check("total > 0（data/sessions.json 有历史会话）", page1["total"] > 0, f"total={page1['total']}")
    check("pageSize 生效", len(page1["items"]) <= 10, f"len={len(page1['items'])}")
    check("扫描窗口显式暴露", page1.get("scanLimit", 0) > 0 and "scanned" in page1,
          f"scanned={page1.get('scanned')}/{page1.get('scanLimit')}")

    if page1["items"]:
        item = page1["items"][0]
        need = {"msgId", "sessionId", "question", "answer", "role", "askedAt", "answeredAt"}
        check("问答项字段齐全（camelCase）", need.issubset(item.keys()),
              f"missing={sorted(need - set(item))}")

    # 排序：时间倒序
    stamps = [r.get("answeredAt") or "" for r in page1["items"]]
    check("按回答时间倒序", stamps == sorted(stamps, reverse=True))

    # 分页不重不漏。注意 msgId 只在会话内自增、**不是全局唯一**，
    # 身份必须用 (sessionId, msgId) 复合键。
    total = page1["total"]
    if total > 10:
        page2 = chatlog_admin.query_qa(page=2, page_size=10)
        ids1 = {(r["sessionId"], r["msgId"]) for r in page1["items"]}
        ids2 = {(r["sessionId"], r["msgId"]) for r in page2["items"]}
        check("第 2 页与第 1 页无重叠", not (ids1 & ids2), f"overlap={len(ids1 & ids2)}")

        # 全量翻页应恰好覆盖 total 条，且无重复
        seen: list[tuple] = []
        pages = (total + 9) // 10
        for pno in range(1, pages + 1):
            seen.extend(
                (r["sessionId"], r["msgId"])
                for r in chatlog_admin.query_qa(page=pno, page_size=10)["items"]
            )
        check("全量翻页无重复且条数 == total",
              len(seen) == total and len(set(seen)) == total,
              f"seen={len(seen)} uniq={len(set(seen))} total={total}")
        check("page 回显正确", page2["page"] == 2)
    else:
        check("第 2 页与第 1 页无重叠（数据量不足，跳过）", True, f"total={total}")

    # 越界页返回空
    far = chatlog_admin.query_qa(page=9999, page_size=10)
    check("越界页返回空列表而非报错", far["items"] == [] and far["total"] == total)
    return page1


def check_filters() -> None:
    from app.services import chatlog_admin

    section("2. 筛选与检索")
    base = chatlog_admin.query_qa(page_size=200)

    # 角色筛选
    roles = sorted({r["role"] for r in base["items"]})
    if roles:
        target = roles[0]
        filtered = chatlog_admin.query_qa(role=target, page_size=200)
        check(f"角色筛选 role={target} 只返回该角色",
              filtered["total"] > 0 and all(r["role"] == target for r in filtered["items"]),
              f"total={filtered['total']}")
        check("角色筛选结果 ⊆ 全量", filtered["total"] <= base["total"],
              f"{filtered['total']} <= {base['total']}")

    # 关键词命中问题
    if base["items"]:
        q = (base["items"][0]["question"] or "").strip()
        if q:
            hit = chatlog_admin.query_qa(keyword=q[:8], page_size=200)
            check("关键词命中问题", hit["total"] >= 1, f"q={q[:8]!r} total={hit['total']}")
            check("命中项确实包含关键词",
                  all(q[:8].lower() in ((r["question"] or "") + (r["answer"] or "")).lower()
                      for r in hit["items"]))

    # 关键词命中回答
    ans = next((r["answer"] for r in base["items"] if (r.get("answer") or "").strip()), "")
    if ans:
        kw = ans.strip()[:10]
        hit = chatlog_admin.query_qa(keyword=kw, page_size=200)
        check("关键词命中回答", hit["total"] >= 1, f"q={kw!r} total={hit['total']}")

    # 无命中
    none = chatlog_admin.query_qa(keyword="绝不可能出现的检索词ZZZZ9999", page_size=200)
    check("无命中时 total=0 且 items 为空", none["total"] == 0 and none["items"] == [])

    # 时间范围
    stamps = [r["answeredAt"] for r in base["items"] if r.get("answeredAt")]
    if stamps:
        day = max(stamps)[:10]
        ranged = chatlog_admin.query_qa(date_from=day, date_to=day, page_size=200)
        check(f"时间范围筛选 dateFrom=dateTo={day} 命中",
              ranged["total"] >= 1 and all((r["answeredAt"] or "").startswith(day)
                                           for r in ranged["items"]),
              f"total={ranged['total']}")
        # 极早的范围应为空
        empty = chatlog_admin.query_qa(date_from="2000-01-01", date_to="2000-01-02", page_size=200)
        check("极早时间范围返回空", empty["total"] == 0, f"total={empty['total']}")

    # 组合筛选：角色 + 关键词
    if base["items"] and roles:
        combo = chatlog_admin.query_qa(role=roles[0], keyword="的", page_size=200)
        check("组合筛选（角色 + 关键词）结果自洽",
              all(r["role"] == roles[0] and "的" in (r["question"] + r["answer"])
                  for r in combo["items"]),
              f"total={combo['total']}")

    # 筛选项
    opts = chatlog_admin.filter_options()
    check("筛选项含 agents/channels/roles",
          all(k in opts for k in ("agents", "channels", "roles")), f"roles={opts['roles']}")
    check("筛选项 roles 非空", len(opts["roles"]) > 0, f"roles={opts['roles']}")


def check_user_view() -> None:
    from app.services import chatlog_admin

    section("3. 咨询用户视图")
    qa = chatlog_admin.query_qa(page_size=200)
    users = chatlog_admin.query_users(page_size=200)

    check("用户视图结构完整", all(k in users for k in ("items", "total", "pageSize")))
    check("会话数 <= 问答数", users["total"] <= qa["total"],
          f"users={users['total']} qa={qa['total']}")
    if users["items"]:
        u = users["items"][0]
        need = {"sessionId", "role", "questionCount", "lastQuestion", "lastAt", "refusedCount"}
        check("用户项字段齐全（camelCase）", need.issubset(u.keys()),
              f"missing={sorted(need - set(u))}")
        check("questionCount >= 1", all(r["questionCount"] >= 1 for r in users["items"]))
        check("sessionId 唯一",
              len({r["sessionId"] for r in users["items"]}) == len(users["items"]))
        check("按末次时间倒序",
              [r["lastAt"] or "" for r in users["items"]]
              == sorted([r["lastAt"] or "" for r in users["items"]], reverse=True))

        # 聚合一致性：同一筛选口径下 问数之和 == 问答总数
        roles = sorted({r["role"] for r in users["items"]})
        if roles:
            qa_r = chatlog_admin.query_qa(role=roles[0], page_size=200)
            us_r = chatlog_admin.query_users(role=roles[0], page_size=200)
            check(f"聚合一致：role={roles[0]} 的问数之和 == 问答总数",
                  sum(r["questionCount"] for r in us_r["items"]) == qa_r["total"],
                  f"sum={sum(r['questionCount'] for r in us_r['items'])} qa={qa_r['total']}")
            check("用户视图角色筛选生效",
                  all(r["role"] == roles[0] for r in us_r["items"]))

        # 末次提问非空
        check("最后提问字段有内容",
              any((r.get("lastQuestion") or "").strip() for r in users["items"]))


def _csv_records(text: str) -> list[list[str]]:
    """按 CSV 语义解析记录：问题/回答里的换行不能当成多行记录。"""
    import csv
    import io

    return list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))


def check_csv() -> None:
    from app.services import chatlog_admin

    section("4. CSV 导出")
    rows, truncated = chatlog_admin.export_rows("qa")
    check("问答导出返回行", len(rows) > 0, f"rows={len(rows)}")
    check("未被截断标记正确（行数 <= EXPORT_LIMIT）",
          truncated == (len(rows) >= chatlog_admin.EXPORT_LIMIT))

    text = chatlog_admin.to_csv("qa", rows)
    check("CSV 带 UTF-8 BOM（Excel 不乱码）", text.startswith("\ufeff"))
    records = _csv_records(text)
    check("CSV 表头 = 问答视图列名",
          records[0] == [name for name, _ in chatlog_admin.QA_CSV_COLUMNS],
          ",".join(records[0])[:60])
    check("CSV 行数 = 表头 + 数据行数", len(records) == len(rows) + 1,
          f"records={len(records)} rows={len(rows)}")

    urows, _ = chatlog_admin.export_rows("users")
    utext = chatlog_admin.to_csv("users", urows)
    urecords = _csv_records(utext)
    check("用户视图 CSV 表头正确",
          urecords[0] == [name for name, _ in chatlog_admin.USER_CSV_COLUMNS])
    check("用户视图 CSV 行数 = 表头 + 数据行数", len(urecords) == len(urows) + 1)

    # 导出与页面同一筛选口径
    qa_page = chatlog_admin.query_qa(page_size=1)
    check("导出全集 == 分页 total（同一筛选口径）",
          len(rows) == qa_page["total"], f"export={len(rows)} total={qa_page['total']}")

    # 带筛选的导出口径一致
    roles = chatlog_admin.filter_options()["roles"]
    if roles:
        frows, _ = chatlog_admin.export_rows("qa", role=roles[0])
        fpage = chatlog_admin.query_qa(role=roles[0], page_size=1)
        check(f"带筛选导出 == 分页 total（role={roles[0]}）",
              len(frows) == fpage["total"], f"export={len(frows)} total={fpage['total']}")

    # 空结果导出仍带表头
    empty_text = chatlog_admin.to_csv("qa", [])
    check("空结果导出仅剩表头", len(empty_text.lstrip("\ufeff").splitlines()) == 1)


# ==================================================================== 5. 接口层
def check_api(port: int) -> None:
    import httpx

    from tools._server import read_log, spawn_server, stop_server, wait_port

    section("5. 接口层契约（真实服务）")
    env = dict(os.environ)
    env["KB_STORE"] = "json"
    env["AUTH_ENABLED"] = "false"
    env["RATE_LIMIT_ENABLED"] = "false"

    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    base = f"http://127.0.0.1:{port}"
    try:
        if not wait_port(port):
            check("服务启动", False, read_log(log_path, 800)[-400:])
            return
        check("服务启动", True, f"port={port}")

        with httpx.Client(timeout=20.0, trust_env=False) as cli:
            r = cli.get(f"{base}/api/admin/chatlogs", params={"pageSize": 5})
            ok = r.status_code == 200 and r.json().get("ok") is True
            check("GET /api/admin/chatlogs 200 + ok", ok, f"status={r.status_code}")
            if ok:
                data = r.json()["data"]
                check("问答接口返回 items/total", "items" in data and "total" in data,
                      f"total={data.get('total')}")

            r = cli.get(f"{base}/api/admin/chatlogs/users", params={"pageSize": 5})
            check("GET /chatlogs/users 200 + ok",
                  r.status_code == 200 and r.json().get("ok") is True, f"status={r.status_code}")

            r = cli.get(f"{base}/api/admin/chatlogs/filters")
            check("GET /chatlogs/filters 200 + ok",
                  r.status_code == 200 and r.json().get("ok") is True, f"status={r.status_code}")

            r = cli.get(f"{base}/api/admin/chatlogs/export", params={"view": "qa"})
            ctype = r.headers.get("content-type", "")
            disp = r.headers.get("content-disposition", "")
            check("GET /chatlogs/export 200 + text/csv",
                  r.status_code == 200 and ctype.startswith("text/csv"),
                  f"status={r.status_code} ctype={ctype}")
            check("导出带 RFC 5987 文件名（中文不乱码）",
                  "filename*=UTF-8''" in disp, disp[:70])
            check("导出带 nosniff", r.headers.get("x-content-type-options") == "nosniff")
            check("导出正文带 BOM", r.text.startswith("\ufeff"), r.text[:8])

            r = cli.get(f"{base}/api/admin/chatlogs/export", params={"view": "users"})
            check("导出用户视图 200 + text/csv",
                  r.status_code == 200 and r.headers.get("content-type", "").startswith("text/csv"))

            # 参数校验
            r = cli.get(f"{base}/api/admin/chatlogs", params={"pageSize": 0})
            check("pageSize=0 → 422（越界拦截）", r.status_code == 422, f"status={r.status_code}")
            r = cli.get(f"{base}/api/admin/chatlogs", params={"pageSize": 999})
            check("pageSize=999 → 422（超上限拦截）", r.status_code == 422, f"status={r.status_code}")
            r = cli.get(f"{base}/api/admin/chatlogs/export", params={"view": "bogus"})
            check("view=bogus → 422（枚举拦截）", r.status_code == 422, f"status={r.status_code}")

            r = cli.get(f"{base}/api/admin/operation-logs")
            check("GET /operation-logs 200 + ok（JSON 模式返回空列表不报错）",
                  r.status_code == 200 and r.json().get("ok") is True, f"status={r.status_code}")
    finally:
        stop_server(proc)


def check_auth(port: int) -> None:
    import httpx

    from tools._server import read_log, spawn_server, stop_server, wait_port

    section("6. 鉴权（AUTH_ENABLED=true）")
    env = dict(os.environ)
    env["KB_STORE"] = "json"
    env["AUTH_ENABLED"] = "true"
    env["RATE_LIMIT_ENABLED"] = "false"
    # **不覆盖 JWT_SECRET**：子进程继承本进程环境，两边用同一密钥签发/校验。
    # 之前给子进程单独设密钥、却在本进程用另一把签 token，导致全部 401。

    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    base = f"http://127.0.0.1:{port}"
    try:
        if not wait_port(port):
            check("鉴权实例启动", False, read_log(log_path, 800)[-400:])
            return
        check("鉴权实例启动", True, f"port={port}")

        with httpx.Client(timeout=20.0, trust_env=False) as cli:
            r = cli.get(f"{base}/api/admin/chatlogs")
            check("无 token → 401", r.status_code == 401, f"status={r.status_code}")
            r = cli.get(f"{base}/api/admin/chatlogs/export")
            check("导出接口同样受保护 → 401", r.status_code == 401, f"status={r.status_code}")

            # 访客 token（非管理员）→ 403
            from app.security.auth import create_token

            guest = create_token("visitor-001", "访客", user_id=None, expires_minutes=5)
            r = cli.get(f"{base}/api/admin/chatlogs",
                        headers={"Authorization": f"Bearer {guest}"})
            check("访客 token → 403（非管理员）", r.status_code == 403, f"status={r.status_code}")

            # 管理员 token → 200
            admin = create_token("admin-001", "管理员", user_id=1, expires_minutes=5)
            r = cli.get(f"{base}/api/admin/chatlogs",
                        headers={"Authorization": f"Bearer {admin}"})
            check("管理员 token → 200", r.status_code == 200, f"status={r.status_code}")
    finally:
        stop_server(proc)


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="会话日志管理验收（任务 2.1）")
    parser.add_argument("--store", default="json", choices=["json", "mysql"],
                        help="存储模式；默认 json（无需 MySQL）")
    parser.add_argument("--port", type=int, default=8021, help="接口层验收用的端口")
    parser.add_argument("--skip-auth", action="store_true", help="跳过鉴权组")
    args = parser.parse_args()

    os.environ["KB_STORE"] = args.store

    from app.config import settings

    print("=" * 68)
    print("会话日志管理验收（任务 2.1）")
    print(f"存储模式：{settings.store_label}    数据目录：{settings.base_dir}")
    print("=" * 68)

    check_qa_view(settings.store_label)
    check_filters()
    check_user_view()
    check_csv()
    check_api(args.port)
    if not args.skip_auth:
        check_auth(args.port + 1)

    print("\n" + "=" * 68)
    print(f"结果：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 68)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
