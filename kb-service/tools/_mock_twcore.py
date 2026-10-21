"""mock twcore —— 站在 ThinkWatch Core 的位置，让**真实网关**能跑通完整链路。

## 为什么需要它

`campus-ai-gateway` 自己不实现路由：请求经 `internal/proxy/twcore.go` 原样转发给
**ThinkWatch Core**（默认 `THINKWATCH_URL=http://host.docker.internal:8788`）。
twcore 是独立的 Rust 项目，本机不方便装（且它最终要连真实厂商 Provider）。

于是把**上游**换成这个 mock：

    能小智  →  nginx:8088  →  gateway:8080  →  mock twcore:8788  →  (假装是模型)

这样能小智面对的是**真实的 campus-ai-gateway**（真实鉴权、真实白名单校验、
真实 RPM/TPM/并发限流、真实审计落库、真实 SSE 透传），
只有最后一步「模型推理」是假的。**这比假网关强一个档次**：
之前 `_fake_gateway.py` 是把整个网关都模拟掉，验证的是能小智的客户端；
本文件让网关的控制面真实参与，验证的是**接入本身**。

## 契约（与 twcore 对上暴露的一致）

* ``POST /v1/chat/completions`` —— OpenAI 兼容；``stream=true`` 时按 SSE 分块下发
* ``GET  /v1/models`` —— 模型列表
* 网关会用 ``THINKWATCH_GATEWAY_KEY`` 覆盖 ``Authorization`` 头，
  所以这里**不校验**该头，但要把它记下来（用于确认网关确实替换了凭据）
* 回显请求里的 ``model``（网关做的是透明转发，别名由控制面维护）

## 绑定地址

默认绑 ``0.0.0.0``：容器要经 ``host.docker.internal`` 访问宿主机端口，
只绑 ``127.0.0.1`` 在部分 Docker Desktop 配置下不可达。
这是**本地联调用的临时服务**，跑完即停。

用法::

    python tools/_mock_twcore.py            # 默认 8788
    python tools/_mock_twcore.py --port 8788
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

#: 记录收到的请求（供验证「网关真的转发了」与「凭据真的被替换了」）
SEEN: list[dict] = []
SEEN_LOCK = threading.Lock()

#: 模型按「流式/非流式」返回的确定性文本
ANSWER = "根据知识库资料[1]，请携带校园卡前往一卡通中心办理补办手续。"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默，避免刷屏
        return

    # -------------------------------------------------------------- 工具
    def _read_body(self) -> bytes:
        """**先读完请求体**再做任何提前返回。

        HTTP/1.1 keep-alive 下，未读完的 body 会残留在连接里污染下一个请求
        （会表现为莫名的 501）。真实 twcore 用 Rust 异步框架不会踩，
        但手写 BaseHTTPRequestHandler 必须自己处理。这条坑在
        `_fake_gateway.py` 里也记过一次。
        """
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Request-Id", f"tw_{int(time.time() * 1000)}")
        self.end_headers()
        self.wfile.write(raw)

    def _record(self, path: str, body: bytes) -> dict:
        try:
            parsed = json.loads(body) if body else {}
        except Exception:  # noqa: BLE001
            parsed = {"_raw": body[:200].decode("utf-8", "replace")}
        rec = {
            "path": path,
            "model": parsed.get("model"),
            "stream": bool(parsed.get("stream")),
            "auth": self.headers.get("Authorization", ""),
            "temperature": parsed.get("temperature"),
            "max_tokens": parsed.get("max_tokens"),
            "has_top_p": "top_p" in parsed,
            "stream_options": parsed.get("stream_options"),
            "at": time.time(),
        }
        with SEEN_LOCK:
            SEEN.append(rec)
        return rec

    # -------------------------------------------------------------- 路由
    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "campus-smart", "object": "model", "owned_by": "twcore-mock"},
                        {"id": "campus-basic", "object": "model", "owned_by": "twcore-mock"},
                    ],
                },
            )
            return
        # 健康/状态查询：方便确认 mock 活着
        if self.path.rstrip("/") in ("/healthz", "/health", "/status"):
            self._json(200, {"ok": True, "service": "mock-twcore", "seen": len(SEEN)})
            return
        self._json(404, {"error": {"code": "not_found", "message": f"no route: {self.path}"}})

    def do_POST(self):  # noqa: N802
        body = self._read_body()
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": {"code": "not_found", "message": f"no route: {self.path}"}})
            return

        rec = self._record(self.path, body)
        model = rec["model"] or "campus-smart"

        if rec["stream"]:
            self._sse(model)
        else:
            self._json(
                200,
                {
                    "id": "chatcmpl-twcore-mock",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": ANSWER},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 128,
                        "completion_tokens": 32,
                        "total_tokens": 160,
                    },
                },
            )

    def _sse(self, model: str) -> None:
        """SSE 分块下发。

        刻意**分块 + 加间隔**：这样 `verify_gateway.py` 的「SSE 是否被整体缓冲」
        断言才有判别力——若链路上某一跳攒批，首个分块会贴近总时长。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(payload: dict) -> None:
            data = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        base = {
            "id": "chatcmpl-twcore-mock",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
        }
        for piece in ("根据知识库资料[1]，", "请携带校园卡", "前往一卡通中心办理补办手续。"):
            chunk({**base, "choices": [{"index": 0, "delta": {"content": piece}}]})
            time.sleep(0.15)
        chunk(
            {
                **base,
                "choices": [],
                "usage": {"prompt_tokens": 128, "completion_tokens": 32, "total_tokens": 160},
            }
        )
        done = b"data: [DONE]\n\n"
        self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def start(host: str = "0.0.0.0", port: int = 8788) -> tuple[ThreadingHTTPServer, str]:
    """在后台线程起服务；返回 ``(server, base_url)``。"""
    server = ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="mock ThinkWatch Core（网关的上游）")
    parser.add_argument("--port", type=int, default=8788, help="监听端口（网关默认连 8788）")
    parser.add_argument("--host", default="0.0.0.0",
                        help="监听地址；容器需经 host.docker.internal 访问，故默认 0.0.0.0")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print("=" * 68)
    print("mock ThinkWatch Core（campus-ai-gateway 的上游）")
    print(f"监听：{args.host}:{args.port}")
    print("网关侧应配 THINKWATCH_URL=http://host.docker.internal:%d" % args.port)
    print("Ctrl+C 停止")
    print("=" * 68)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
