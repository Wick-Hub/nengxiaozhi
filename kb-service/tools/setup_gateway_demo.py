"""在 campus-ai-gateway 上快速铺一套「能小智可用」的配置（幂等）。

## 用途

能小智接入网关前，网关侧必须先有：**租户 → Provider → 虚拟模型 →（自动主路由）→ 业务 Key**，
且业务 Key 的 ``AllowedModels`` 要与能小智填的 ``remoteModel`` **逐字一致**
（不一致就是 403 ``model_not_allowed``，这是接入后最容易踩的坑）。

本脚本把这一串建好，并**打印能小智侧要填的三项配置**。

## 幂等

重复执行不会建重复资源：已存在的租户/Provider/模型会复用（按 code / name / alias 匹配）。
**业务 Key 例外**——网关只在创建时返回一次明文 Key，已存在的 Key 无法取回明文，
因此已存在时脚本只列出前缀，并提示需要新建或换名。

用法::

    # gateway 默认是 session 模式（Admin UI 也要求它）→ 用账号密码
    python tools/setup_gateway_demo.py --base-url http://127.0.0.1:8088 \
        --admin-user admin --admin-password '<密码>'

    # 仅当 ADMIN_AUTH_MODE=token 时用：
    python tools/setup_gateway_demo.py --base-url http://127.0.0.1:8088 \
        --admin-token <ADMIN_TOKEN>

    # 常用可选参数
    --tenant-code nengxiaozhi --tenant-name 能小智校园AI助手
    --provider qwen-bailian --upstream qwen-max
    --model campus-smart          # 这个别名就是能小智要填的 remoteModel
    --key-name nengxiaozhi-app

⚠ 本脚本面向联调/演示。生产环境应由管理员在 Admin UI 上按同一顺序配置。
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def call(
    cli: httpx.Client,
    method: str,
    path: str,
    *,
    token: str,
    body: dict | None = None,
) -> tuple[int, dict]:
    """打一次 Admin API，返回 ``(status, payload)``；错误不抛，交给调用方判断。

    ``token`` 为空时**不下发 Authorization 头** —— 登录接口 ``/admin/login``
    本身不需要凭据，而发一个空的 ``Bearer ``（尾部空格）会让 httpx 抛
    ``LocalProtocolError: Illegal header value``（不是服务端拒绝，是客户端就发不出去）。
    """
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = cli.request(method, path, headers=headers, json=body)
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = {"_raw": resp.text[:300]}
    return resp.status_code, data if isinstance(data, dict) else {"data": data}


def ensure_tenant(cli: httpx.Client, token: str, code: str, name: str) -> dict:
    status, data = call(cli, "GET", "/admin/tenants", token=token)
    for t in (data.get("data") or []):
        if t.get("code") == code:
            print(f"  · 租户已存在：{code} → {t['id']}")
            return t
    status, data = call(cli, "POST", "/admin/tenants", token=token,
                        body={"code": code, "name": name})
    if status not in (200, 201):
        raise SystemExit(f"建租户失败 HTTP {status}：{json.dumps(data, ensure_ascii=False)}")
    print(f"  ✓ 租户已建：{code} → {data['id']}")
    return data


def ensure_provider(cli: httpx.Client, token: str, *, name: str, base_url: str,
                    protocol: str, credential_env: str) -> dict:
    status, data = call(cli, "GET", "/admin/providers", token=token)
    for p in (data.get("data") or []):
        if p.get("name") == name:
            print(f"  · Provider 已存在：{name} → {p['id']}")
            return p
    status, data = call(cli, "POST", "/admin/providers", token=token, body={
        "name": name, "base_url": base_url,
        "protocol": protocol, "credential_env": credential_env,
    })
    if status not in (200, 201):
        raise SystemExit(f"建 Provider 失败 HTTP {status}：{json.dumps(data, ensure_ascii=False)}")
    print(f"  ✓ Provider 已建：{name} → {data['id']}")
    return data


def ensure_model(cli: httpx.Client, token: str, *, alias: str, provider_id: str,
                 upstream_model: str) -> dict:
    status, data = call(cli, "GET", "/admin/models", token=token)
    for m in (data.get("data") or []):
        if m.get("alias") == alias:
            print(f"  · 虚拟模型已存在：{alias} → {m['id']}")
            return m
    status, data = call(cli, "POST", "/admin/models", token=token, body={
        "alias": alias, "provider_id": provider_id, "upstream_model": upstream_model,
    })
    if status not in (200, 201):
        raise SystemExit(f"建虚拟模型失败 HTTP {status}：{json.dumps(data, ensure_ascii=False)}")
    print(f"  ✓ 虚拟模型已建：{alias} → {data['id']}")

    # ⚠ 网关在创建模型时会**自动生成一条 priority=1 的主路由**
    #   （migrations/0003 的 backfill 逻辑 + 唯一索引 (model_id, provider_id, upstream_model)）。
    #   再手动 POST 同一绑定会 409 model_route_create_failed —— 这是正常行为，不是错误。
    routes = call(cli, "GET", f"/admin/models/{data['id']}/routes", token=token)[1]
    items = routes.get("data") or []
    if items:
        print(f"  · 已自动生成主路由：priority={items[0].get('priority')} "
              f"weight={items[0].get('weight')} timeout={items[0].get('timeout_ms')}ms")
    else:
        status, rdata = call(cli, "POST", f"/admin/models/{data['id']}/routes", token=token,
                            body={"provider_id": provider_id, "upstream_model": upstream_model,
                                  "priority": 1, "weight": 100, "timeout_ms": 60000})
        print(f"  ✓ 手动补建主路由（HTTP {status}）")
    return data


def ensure_api_key(cli: httpx.Client, token: str, *, tenant_id: str, name: str,
                   allowed_models: list[str], rpm: int, tpm: int, concurrency: int) -> str:
    """返回明文 Key；已存在时返回空串（明文只在创建响应里出现一次）。"""
    status, data = call(cli, "GET", "/admin/api-keys", token=token)
    for k in (data.get("data") or []):
        if k.get("name") == name:
            print(f"  ! 业务 Key「{name}」已存在（前缀 {k.get('key_prefix')}），明文不可取回")
            print("    → 如需新 Key，请换 --key-name，或在 Admin UI 上轮换")
            return ""
    status, data = call(cli, "POST", "/admin/api-keys", token=token, body={
        "tenant_id": tenant_id, "name": name,
        "allowed_models": allowed_models,
        "rpm_limit": rpm, "tpm_limit": tpm, "max_concurrency": concurrency,
    })
    if status not in (200, 201):
        raise SystemExit(f"建业务 Key 失败 HTTP {status}：{json.dumps(data, ensure_ascii=False)}")
    key = data.get("api_key") or ""
    print(f"  ✓ 业务 Key 已建：{name}（前缀 {data.get('key_prefix')}）")
    return key


def resolve_admin_token(cli: httpx.Client, *, admin_token: str,
                        user: str, password: str) -> str:
    """拿到一个可用于 Admin API 的 bearer 凭据。**两种认证模式都要支持。**

    网关有两条管理面认证路径（见其 ``admin_auth.go``）：

    * **token 模式** —— 直接用 ``ADMIN_TOKEN``；
    * **session 模式**（**默认**）—— 必须用账号密码换 access token。

    ⚠ 只支持 token 模式是不够的：网关默认就是 session 模式，而且它的
    **Admin UI 在 token 模式下不提供登录表单**（源码注释：console never stores
    a credential），所以要看控制台就必须用 session 模式。工具得跟上。
    """
    if admin_token:
        return admin_token
    if not (user and password):
        raise SystemExit(
            "需要凭据：请用 --admin-token（token 模式），"
            "或 --admin-user/--admin-password（session 模式，网关默认）"
        )
    status, data = call(cli, "POST", "/admin/login",
                        token="", body={"username": user, "password": password})
    token = str(data.get("access_token") or "")
    if status != 200 or not token:
        raise SystemExit(
            f"管理员登录失败 HTTP {status}：{json.dumps(data, ensure_ascii=False)[:200]}"
        )
    print(f"  ✓ 已用账号 {user} 登录（session 模式）")
    return token


def main() -> int:
    p = argparse.ArgumentParser(description="在网关铺一套能小智可用配置（幂等）")
    p.add_argument("--base-url", required=True, help="网关地址，如 http://127.0.0.1:8088（不要带 /v1）")
    p.add_argument("--admin-token", default="",
                   help="网关管理员 token（仅 token 认证模式可用）")
    p.add_argument("--admin-user", default="",
                   help="网关管理员账号（session 认证模式：用账号密码换 access token）")
    p.add_argument("--admin-password", default="",
                   help="网关管理员密码（session 认证模式）")
    p.add_argument("--tenant-code", default="nengxiaozhi")
    p.add_argument("--tenant-name", default="能小智校园AI助手")
    p.add_argument("--provider", default="qwen-bailian")
    p.add_argument("--provider-base-url",
                   default="https://dashscope.aliyuncs.com/compatible-mode/v1")
    p.add_argument("--provider-protocol", default="openai-chat",
                   choices=["openai-chat", "openai-responses", "anthropic", "gemini"])
    p.add_argument("--credential-env", default="DASHSCOPE_API_KEY",
                   help="网关容器内保存该厂商凭据的环境变量名（不是明文 Key）")
    p.add_argument("--upstream", default="qwen-max", help="厂商侧真实模型名")
    p.add_argument("--model", default="campus-smart", help="虚拟模型别名（能小智填的 remoteModel）")
    p.add_argument("--key-name", default="nengxiaozhi-app")
    p.add_argument("--rpm", type=int, default=600)
    p.add_argument("--tpm", type=int, default=200000)
    p.add_argument("--concurrency", type=int, default=20)
    p.add_argument("--no-verify-ssl", action="store_true")
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    if base.endswith("/v1"):
        print("! --base-url 不应带 /v1（本节调 Admin API，数据面才用 /v1）")
        base = base[:-3]
    trust = base.split("//")[-1].split("/")[0].split(":")[0] not in (
        "127.0.0.1", "localhost", "::1", "0.0.0.0")

    print("=" * 72)
    print("在 campus-ai-gateway 上铺一套能小智可用配置")
    print(f"网关：{base}")
    print("=" * 72)

    # base_url 必须设上：call() 里用的是 "/admin/xxx" 相对路径
    with httpx.Client(
        base_url=base, timeout=30.0, trust_env=trust, verify=not args.no_verify_ssl
    ) as cli:
        # 先确定管理员凭据（token 模式直接用；session 模式用账号密码换 token）
        try:
            token = resolve_admin_token(
                cli, admin_token=args.admin_token,
                user=args.admin_user, password=args.admin_password,
            )
        except SystemExit as exc:
            print(f"\n✗ {exc}")
            return 1

        status, data = call(cli, "GET", "/admin/status", token=token)
        if status != 200:
            print(f"\n✗ Admin API 不可用（HTTP {status}）：{json.dumps(data, ensure_ascii=False)[:300]}")
            print("  请确认：① 网关已启动 ② 凭据正确")
            print("  网关默认是 session 模式 → 需 --admin-user/--admin-password；")
            print("  仅当 ADMIN_AUTH_MODE=token 时才用 --admin-token。")
            return 1
        print(f"\n网关状态：{json.dumps(data, ensure_ascii=False)}")

        print("\n[1/5] 租户")
        tenant = ensure_tenant(cli, token, args.tenant_code, args.tenant_name)

        print("\n[2/5] Provider")
        provider = ensure_provider(
            cli, token, name=args.provider, base_url=args.provider_base_url,
            protocol=args.provider_protocol, credential_env=args.credential_env,
        )

        print("\n[3/5] 虚拟模型（含自动主路由）")
        model = ensure_model(
            cli, token, alias=args.model,
            provider_id=provider["id"], upstream_model=args.upstream,
        )

        print("\n[4/5] 业务 Key")
        plain = ensure_api_key(
            cli, token, tenant_id=tenant["id"], name=args.key_name,
            allowed_models=[args.model], rpm=args.rpm, tpm=args.tpm,
            concurrency=args.concurrency,
        )

        print("\n[5/5] 校验白名单与模型别名是否逐字一致")
        status, data = call(cli, "GET", "/admin/api-keys", token=token)
        mine = next((k for k in (data.get("data") or []) if k.get("name") == args.key_name), None)
        if mine and args.model in (mine.get("allowed_models") or []):
            print(f"  ✓ AllowedModels 含 {args.model}")
        else:
            print(f"  ✗ AllowedModels 不含 {args.model}：{mine.get('allowed_models') if mine else '未找到 Key'}")
            print("    → 这会导致 403 model_not_allowed")

    print("\n" + "=" * 72)
    print("能小智侧要填的三项：")
    print("=" * 72)
    print(f"  apiBase      = {base}/v1")
    print(f"  remoteModel  = {args.model}")
    if plain:
        print(f"  apiKey       = {plain}")
        print("\n  ⚠ 明文 Key 只在创建时返回这一次，请立即保存。")
    else:
        print("  apiKey       = （已存在的 Key 无法取回明文，请新建或从 Admin UI 轮换）")
    print("\n填入方式（二选一）：")
    print("  A. 管理后台「模型管理」新建模型，把上面三项填进去，再绑定给助手")
    print("  B. kb-service/.env：")
    print(f"       LLM_BASE_URL = {base}/v1")
    print(f"       LLM_MODEL    = {args.model}")
    print("       LLM_API_KEY  = <上面的 apiKey>")
    print("\n验证：")
    print("  python tools/verify_gateway.py --self-test")
    print(f"  python tools/verify_gateway_e2e.py --base-url {base}/v1 \\")
    print(f"      --api-key <apiKey> --model {args.model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
