"""角色权限与管理账号验收（任务 2.9 / M-SYS-01·03）。

真实 MySQL + ``AUTH_ENABLED=true``，覆盖「角色矩阵可配置、菜单/按钮权限
随角色生效、越权 403 且留痕」三条验收口径：

1. 目录与种子：38 个权限点、管理员登录恒有全部权限；
2. 角色矩阵：新建角色 → 清单可见 → 权限点变更对**同一 token** 即时生效；
3. 管理员账号：新建必须设密码；错误密码 401；绑定自定义管理角色可登录；
4. 越权：无权限模块返回 403，并写入 ``ACCESS_DENIED`` 操作日志；
5. 保护规则：内置角色不可删、至少保留一个启用管理员、不能停用当前账号；
6. 清理：删除本次创建的账号与角色（``--keep`` 可保留排查）。

前置：MySQL 已启动，已执行 ``tools/migrate_rbac_2_9.py`` 与 ``tools/seed_auth.py``。

用法::

    python tools/verify_rbac.py
    python tools/verify_rbac.py --keep
"""

from __future__ import annotations

import argparse
import os
import random
import string
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

# 必须在导入 app.* 之前定型（settings 在导入期读取环境变量）
os.environ.setdefault("KB_STORE", "mysql")
os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("AUTH_ENABLED", "true")
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


def _login(client, user_no: str, password: str = "") -> dict:
    body = {"userNo": user_no}
    if password:
        body["password"] = password
    resp = client.post("/api/auth/login", json=body)
    return {"status": resp.status_code, "json": resp.json()}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="角色权限 API 验收（任务 2.9）")
    parser.add_argument("--keep", action="store_true", help="保留本次创建的账号/角色")
    args = parser.parse_args()

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    if settings.store_label != "mysql":
        print("❌ KB_STORE 必须为 mysql（真实库验收）")
        return 1

    client = TestClient(app)
    suffix = "".join(random.choices(string.ascii_lowercase, k=5))
    role_code = f"verify_{suffix}"
    role_name = f"验收角色{suffix}"
    user_no = f"V{suffix}".upper()
    password = "Verify#2026"
    role_id = None
    user_id = None

    print(f"数据库：{settings.store_label} ｜ AUTH_ENABLED={settings.auth_enabled}")

    try:
        section("1. 目录与管理员登录")
        unauth = client.get("/api/admin/roles")
        check("未登录访问角色接口 401", unauth.status_code == 401, str(unauth.status_code))

        login = _login(client, "admin")
        if not (login["status"] == 200 and login["json"].get("ok")):
            print(f"❌ 演示管理员登录失败：{login['json']}")
            print("   请先执行 tools/seed_auth.py（admin / 免密）")
            return 1
        check("演示管理员免密登录成功", True, "200")
        admin_data = login["json"]["data"]
        admin_token = admin_data["token"]
        check("管理员 isAdmin/isManager 均为真",
              admin_data["isAdmin"] and admin_data["isManager"], str(admin_data.get("role")))
        check("管理员拥有全部 38 个权限点", len(admin_data["permissions"]) == 38,
              str(len(admin_data["permissions"])))

        roles = client.get("/api/admin/roles", headers=_auth(admin_token)).json()["data"]
        check("权限目录为 17 个模块", len(roles["catalog"]) == 17, str(len(roles["catalog"])))
        check("MySQL 模式可编辑角色矩阵", roles["editable"] is True)
        admin_role = next((r for r in roles["items"] if r["roleCode"] == "admin"), None)
        check("内置管理员角色存在且只读", bool(admin_role and admin_role["locked"]))

        section("2. 新建角色与账号")
        created_role = client.post(
            "/api/admin/roles",
            headers=_auth(admin_token),
            json={
                "roleCode": role_code,
                "roleName": role_name,
                "remark": "自动化验收临时角色",
                "permissions": ["docs.view", "points.view"],
            },
        )
        check("新建角色 200", created_role.status_code == 200, created_role.text[:160])
        role_id = created_role.json()["data"]["roleId"]
        check("角色权限为 2 个", len(created_role.json()["data"]["permissions"]) == 2)

        dup = client.post(
            "/api/admin/roles",
            headers=_auth(admin_token),
            json={"roleCode": role_code, "roleName": role_name + "x", "permissions": []},
        )
        check("重复角色编码 409", dup.status_code == 409, str(dup.status_code))

        created_admin = client.post(
            "/api/admin/admins",
            headers=_auth(admin_token),
            json={
                "userNo": user_no,
                "realName": f"验收账号{suffix}",
                "password": password,
                "roleIds": [role_id],
                "status": 1,
            },
        )
        check("新建管理员 200", created_admin.status_code == 200, created_admin.text[:160])
        user_id = created_admin.json()["data"]["userId"]
        check("新账号标记为已设密码", created_admin.json()["data"]["hasPassword"] is True)

        no_pass = _login(client, user_no)
        check("已设密码账号缺密码登录 401", no_pass["status"] == 401, str(no_pass["status"]))
        bad_pass = _login(client, user_no, "wrong-password")
        check("错误密码登录 401", bad_pass["status"] == 401, str(bad_pass["status"]))
        login2 = _login(client, user_no, password)
        check("正确密码登录成功", login2["status"] == 200 and login2["json"]["ok"],
              str(login2["status"]))
        limited = login2["json"]["data"]
        limited_token = limited["token"]
        check("自定义管理角色可登录且 isManager 为真", limited["isManager"] is True,
              str(limited.get("role")))
        check("登录返回权限点与角色一致",
              set(limited["permissions"]) == {"docs.view", "points.view"},
              str(limited["permissions"]))

        section("3. 菜单/接口权限随矩阵生效")
        check("有 docs.view：文档列表 200",
              client.get("/api/docs", headers=_auth(limited_token)).status_code == 200)
        check("无 points.edit：新建知识点 403",
              client.post("/api/knowledge/points", headers=_auth(limited_token),
                          json={"docId": "D001", "content": "x"}).status_code == 403)
        denied = client.get("/api/admin/operation-logs", headers=_auth(limited_token))
        check("无 system.logs：操作日志 403", denied.status_code == 403, str(denied.status_code))
        check("403 响应含可读权限说明", "操作日志" in str(denied.json().get("detail")),
              str(denied.json().get("detail"))[:80])

        logs = client.get(
            "/api/admin/operation-logs",
            headers=_auth(admin_token),
            params={"action": "ACCESS_DENIED", "pageSize": 100},
        ).json()["data"]
        hit = [x for x in logs["items"]
               if x.get("targetId") == "/api/admin/operation-logs"]
        check("越权写入 ACCESS_DENIED 操作日志", bool(hit), f"matched={len(hit)}")
        if hit:
            detail = hit[0].get("detail") or {}
            check("日志含所需权限点与角色",
                  detail.get("required") == "system.logs" and bool(detail.get("role")),
                  str(detail)[:120])

        before = client.get("/api/admin/chatlogs", headers=_auth(limited_token))
        check("改矩阵前会话日志 403", before.status_code == 403)
        patched = client.patch(
            f"/api/admin/roles/{role_id}",
            headers=_auth(admin_token),
            json={"permissions": ["docs.view", "points.view", "chatlogs.view"]},
        )
        check("更新角色矩阵 200", patched.status_code == 200, patched.text[:120])
        after = client.get("/api/admin/chatlogs", headers=_auth(limited_token))
        check("同一 token 立即获得 chatlogs.view（无需重登）", after.status_code == 200,
              str(after.status_code))

        me = client.get("/api/auth/me", headers=_auth(limited_token)).json()["data"]
        check("auth/me 权限点实时更新", "chatlogs.view" in me["permissions"],
              str(me["permissions"]))

        section("4. 保护规则")
        del_builtin = client.delete(
            f"/api/admin/roles/{admin_role['roleId'] if admin_role else 1}",
            headers=_auth(admin_token),
        )
        check("不能删除内置管理员角色",
              del_builtin.status_code in (400, 404), str(del_builtin.status_code))
        admins = client.get("/api/admin/admins", headers=_auth(admin_token)).json()["data"]
        admin_me = next((a for a in admins["items"] if a["userNo"] == "admin"), None)
        check("管理员清单包含 admin 账号", admin_me is not None)
        if admin_me:
            self_off = client.patch(
                f"/api/admin/admins/{admin_me['userId']}",
                headers=_auth(admin_token),
                json={"status": 0},
            )
            check("不能停用当前登录账号", self_off.status_code == 400,
                  self_off.text[:120])
            self_rm = client.delete(
                f"/api/admin/admins/{admin_me['userId']}", headers=_auth(admin_token)
            )
            check("不能移除当前登录账号", self_rm.status_code == 400, self_rm.text[:120])
    finally:
        if not args.keep and user_id is not None:
            client.delete(f"/api/admin/admins/{user_id}", headers=_auth(admin_token))
        if not args.keep and role_id is not None:
            client.delete(f"/api/admin/roles/{role_id}", headers=_auth(admin_token))

    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
