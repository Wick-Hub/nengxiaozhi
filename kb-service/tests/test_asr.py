"""语音识别（任务 4.7 / C-04·P-QA-01）测试。

覆盖：

1. 能力状态：mock 可用、关闭开关、配置真实 provider 但缺 Key；
2. mock 转写：默认/自定义文本、空结果、超长截断；
3. 上传校验：空音频、格式白名单、大小上限；
4. 真实 provider：OpenAI 兼容 multipart 契约、上游异常映射、响应体不外泄；
5. 与问答链路衔接：转写文本直接作为 `/api/chat` 的问题。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import asr

client = TestClient(app)


def _upload(data: bytes = b"fake-webm-audio", name: str = "voice.webm", mime: str = "audio/webm"):
    return client.post("/api/asr/transcribe", files={"file": (name, data, mime)})


# ------------------------------------------------------------------ 状态与 mock
def test_status_defaults_to_mock() -> None:
    resp = client.get("/api/asr/status")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["enabled"] is True
    assert data["ready"] is True
    assert data["provider"] == "mock"
    assert data["mock"] is True
    assert data["maxSeconds"] == 60
    assert data["maxTranscriptChars"] == 500
    assert "apiKey" not in data and "api_key" not in data


def test_status_hides_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "asr_enabled", False)
    data = client.get("/api/asr/status").json()["data"]
    assert data["enabled"] is False
    assert data["ready"] is False
    resp = _upload()
    assert resp.status_code == 503


def test_status_openai_without_key_is_not_ready(monkeypatch) -> None:
    monkeypatch.setattr(settings, "asr_provider", "openai")
    monkeypatch.setattr(settings, "asr_api_key", "")
    data = client.get("/api/asr/status").json()["data"]
    assert data["enabled"] is False
    assert data["ready"] is False
    assert _upload().status_code == 503


def test_mock_transcribe_returns_fixed_text() -> None:
    resp = _upload()
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["text"] == settings.asr_mock_text
    assert data["provider"] == "mock"
    assert data["truncated"] is False
    assert data["elapsedMs"] >= 0


def test_mock_transcribe_custom_text_and_truncation(monkeypatch) -> None:
    monkeypatch.setattr(settings, "asr_mock_text", "  请  问   校园网  ")
    assert _upload().json()["data"]["text"] == "请 问 校园网"
    monkeypatch.setattr(settings, "asr_mock_text", "测" * 600)
    data = _upload().json()["data"]
    assert len(data["text"]) == 500
    assert data["truncated"] is True


def test_mock_empty_text_is_422(monkeypatch) -> None:
    monkeypatch.setattr(settings, "asr_mock_text", "   ")
    resp = _upload()
    assert resp.status_code == 422
    assert "未识别到语音内容" in resp.json()["detail"]


# ------------------------------------------------------------------ 上传校验
def test_empty_audio_rejected() -> None:
    resp = _upload(b"")
    assert resp.status_code == 400
    assert "音频为空" in resp.json()["detail"]


def test_unsupported_type_rejected() -> None:
    resp = _upload(b"hello", "note.txt", "text/plain")
    assert resp.status_code == 415


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("voice.webm", "audio/webm;codecs=opus"),
        ("VOICE.WAV", "application/octet-stream"),
        ("voice", "audio/mp4"),
        ("voice.m4a", ""),
    ],
)
def test_supported_types(name: str, mime: str) -> None:
    assert _upload(b"x" * 32, name, mime).status_code == 200


def test_oversize_rejected(monkeypatch) -> None:
    monkeypatch.setattr(settings, "asr_max_bytes", 8)
    resp = _upload(b"x" * 9)
    assert resp.status_code == 413


# ------------------------------------------------------------------ 真实 provider
class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, body: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = body

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    """记录请求并按预设返回；替代 httpx.Client 的上下文管理器。"""

    last: dict = {}
    response: _FakeResponse = _FakeResponse(payload={"text": "你好"})
    error: Exception | None = None

    def __init__(self, timeout=None) -> None:
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def post(self, url, headers=None, data=None, files=None):
        _FakeClient.last = {"url": url, "headers": headers, "data": data, "files": files}
        if _FakeClient.error:
            raise _FakeClient.error
        return _FakeClient.response


@pytest.fixture
def openai_mode(monkeypatch):
    monkeypatch.setattr(settings, "asr_provider", "openai")
    monkeypatch.setattr(settings, "asr_api_key", "test-asr-key")
    monkeypatch.setattr(settings, "asr_base_url", "https://asr.example.com/v1/")
    monkeypatch.setattr(settings, "asr_model", "whisper-1")
    monkeypatch.setattr(settings, "asr_language", "zh")
    monkeypatch.setattr(asr.httpx, "Client", _FakeClient)
    _FakeClient.error = None
    _FakeClient.response = _FakeResponse(payload={"text": "语音转出来的问题"})
    return _FakeClient


def test_openai_provider_multipart_contract(openai_mode) -> None:
    resp = _upload(b"audio-bytes", "voice.webm", "audio/webm;codecs=opus")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["text"] == "语音转出来的问题"
    assert data["provider"] == "openai"
    assert data["truncated"] is False
    assert data["elapsedMs"] >= 0
    call = openai_mode.last
    assert call["url"] == "https://asr.example.com/v1/audio/transcriptions"
    assert call["headers"]["Authorization"] == "Bearer test-asr-key"
    assert call["data"]["model"] == "whisper-1"
    assert call["data"]["language"] == "zh"
    filename, audio, mime = call["files"]["file"]
    assert filename == "voice.webm"
    assert audio == b"audio-bytes"
    assert mime == "audio/webm"


def test_openai_upstream_error_is_generic(openai_mode) -> None:
    openai_mode.response = _FakeResponse(status_code=500, body="secret upstream detail")
    resp = _upload()
    assert resp.status_code == 502
    assert "secret" not in resp.text
    assert "语音识别服务返回错误" in resp.json()["detail"]


def test_openai_timeout_maps_to_504(openai_mode) -> None:
    openai_mode.error = httpx.TimeoutException("slow")
    resp = _upload()
    assert resp.status_code == 504
    assert "超时" in resp.json()["detail"]


def test_openai_bad_json_is_502(openai_mode) -> None:
    openai_mode.response = _FakeResponse(payload=ValueError("not json"))
    assert _upload().status_code == 502


def test_openai_rejects_unsafe_base_url(openai_mode, monkeypatch) -> None:
    monkeypatch.setattr(settings, "asr_base_url", "file:///etc")
    resp = _upload()
    assert resp.status_code == 503
    assert "地址不合法" in resp.json()["detail"]


# ------------------------------------------------------------------ 同一问答链路
def test_transcript_goes_through_chat_pipeline() -> None:
    text = _upload().json()["data"]["text"]
    resp = client.post(
        "/api/chat",
        json={"question": text, "role": "本科生"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["sessionId"]
    assert body["data"]["answer"]
