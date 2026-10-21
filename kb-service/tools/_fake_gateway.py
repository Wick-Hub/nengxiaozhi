"""campus-ai-gateway 的**假端点**（仅供 `tools/verify_gateway.py --self-test` 使用）。

按网关源码的对外契约实现，用于在没有真实网关的机器上验证 `verify_gateway.py`
的**在线分支**真的能跑通——否则那 11 条断言就是「写了但从未执行过」。

依据的契约（与其源码逐条对应）：

* ``GET /v1/models`` → 配了 ``AllowedModels`` 时直接返回白名单，**不代理上游**
* ``POST /v1/chat/completions`` → 鉴权 → 白名单校验 → 透传
* 错误体 ``{"error":{"code","message","request_id"}}``（``internal/httpx/response.go``）
* ``/v1/chat/completions`` 要求 ``model`` 非空，否则 400 ``model_required``
* 白名单外的 model → 403 ``model_not_allowed``
* SSE 分块下发（模拟 ``FlushInterval = -1``，不被整体缓冲）

## 一个必须记住的坑

**POST 分支必须先把请求体读完，再做任何提前返回。** HTTP/1.1 keep-alive 下，
鉴权失败就直接响应会让未读的 body 残留到下一个请求的解析里 ——
表现为**第二个请求收到 501 Unsupported method**，而 curl 手测完全正常
（curl 每次新建连接）。`httpx.Client` 复用连接才会暴露。

真实网关用 Go 的 ``net/http``，handler 返回后框架会自动 drain 未读 body，
**不存在这个问题**；这个坑只属于本假端点。首次实现时就踩到了，故留此注释。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: 假网关认可的业务 Key
VALID_KEY = "sk-campus-probe-key"
#: 模拟某个 Key 的 AllowedModels 白名单
WHITELIST = {"campus-smart", "campus-basic"}
#: 固定回显的 request_id（真实网关是 uuid）
REQUEST_ID = "req_probe_0001"

#: 网关错误码 → 中文归因（与 verify_gateway.GATEWAY_ERRORS 保持一致的含义）
ERROR_CODES = {
    401: "invalid_api_key",
    400: "model_required",
    403: "model_not_allowed",
    404: "not_found",
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):  # 静默
        return

    # -------------------------------------------------------------- 响应
    def _send(self, status: int, payload: dict, request_id: str = REQUEST_ID) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if request_id:
            self.send_header("X-Request-Id", request_id)
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, status: int, message: str, code: str = "") -> None:
        self._send(
            status,
            {
                "error": {
                    "code": code or ERROR_CODES.get(status, "error"),
                    "message": message,
                    "request_id": REQUEST_ID,
                }
            },
        )

    def _auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self._err(401, "Missing bearer token")
            return False
        if auth[7:].strip() != VALID_KEY:
            self._err(401, "Invalid API key")
            return False
        return True

    # -------------------------------------------------------------- 路由
    def do_GET(self):  # noqa: N802
        if self.path != "/v1/models":
            self._err(404, "no such route")
            return
        if not self._auth():
            return
        self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in sorted(WHITELIST)]})

    def do_POST(self):  # noqa: N802
        # 先读完 body —— 见模块 docstring「一个必须记住的坑」。顺序不可调换。
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        if self.path != "/v1/chat/completions":
            self._err(404, "no such route")
            return
        if not self._auth():
            return

        body = json.loads(raw or b"{}")
        model = str(body.get("model") or "")
        if not model:
            self._err(400, "Model is required")
            return
        if model not in WHITELIST:
            self._err(403, "Model is not allowed for this API key")
            return

        if body.get("stream"):
            self._sse(model)
        else:
            self._send(
                200,
                {
                    "id": "chatcmpl-probe",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "收到"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
                },
            )

    def _sse(self, model: str) -> None:
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

        base = {"id": "chatcmpl-probe", "object": "chat.completion.chunk", "model": model}
        for piece in ("收", "到", "了"):
            chunk({**base, "choices": [{"index": 0, "delta": {"content": piece}}]})
            time.sleep(0.2)  # 制造间隔，好让「是否被整体缓冲」可判定
        chunk(
            {
                **base,
                "choices": [],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            }
        )
        done = b"data: [DONE]\n\n"
        self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def start(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """在当前进程内起假网关线程。

    :return: ``(server, base_url)``；``base_url`` 形如 ``http://127.0.0.1:PORT/v1``
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


def stop(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()
