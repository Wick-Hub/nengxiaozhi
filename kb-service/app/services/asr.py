"""语音识别适配器（任务 4.7）。

外部语音识别接口未就绪时使用 ``ASR_PROVIDER=mock``：不访问网络，固定返回
``ASR_MOCK_TEXT``，让「录音 → 转写 → 同一问答链路」可以端到端联调与验收。
接口就绪后改为 ``ASR_PROVIDER=openai``，按 OpenAI 兼容的
``POST {ASR_BASE_URL}/audio/transcriptions`` 调用（multipart，字段 ``file`` /
``model`` / ``language``），无需改动前端。

安全与隐私：

- 只接收白名单内的音频 MIME/扩展名，大小由 ``ASR_MAX_BYTES`` 限制；
- 不记录音频内容，也不把上游响应体原样回传（可能含内部错误页）；
- 转写文本截断到问答接口允许的 500 字，并返回 ``truncated`` 供前端提示；
- ``ASR_API_KEY`` 只进请求头，不出现在接口响应或日志里。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MAX_TRANSCRIPT_CHARS = 500

#: 浏览器 MediaRecorder 常见产物 + 常见音频文件；MIME 比较时忽略 ``;codecs=...``
SUPPORTED_MIME = {
    "audio/webm",
    "audio/ogg",
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/mpeg",
    "audio/mp4",
    "audio/aac",
    "audio/flac",
}
SUPPORTED_EXTS = {"webm", "ogg", "oga", "wav", "mp3", "m4a", "mp4", "aac", "flac"}

#: 前端展示用；真正的录制能力由浏览器决定
ACCEPT = "audio/webm,audio/ogg,audio/wav,audio/mpeg,audio/mp4,audio/aac,audio/flac"


class AsrError(Exception):
    """语音识别失败，``status_code`` 直接用于 HTTP 响应。"""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class Transcript:
    text: str
    provider: str
    truncated: bool
    elapsed_ms: int


def is_mock() -> bool:
    return settings.asr_provider == "mock"


def ready() -> bool:
    """是否具备识别条件：mock 恒可用；openai 需要 API Key。"""
    if not settings.asr_enabled:
        return False
    return is_mock() or bool(settings.asr_api_key.strip())


def status() -> dict:
    return {
        "enabled": bool(settings.asr_enabled) and ready(),
        "ready": ready(),
        "provider": settings.asr_provider,
        "mock": is_mock(),
        "model": "" if is_mock() else settings.asr_model,
        "language": settings.asr_language,
        "maxBytes": settings.asr_max_bytes,
        "maxSeconds": settings.asr_max_seconds,
        "accept": ACCEPT,
        "maxTranscriptChars": MAX_TRANSCRIPT_CHARS,
    }


def normalize_mime(content_type: str | None) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def is_supported(filename: str | None, content_type: str | None) -> bool:
    mime = normalize_mime(content_type)
    if mime in SUPPORTED_MIME or mime == "application/octet-stream":
        return True
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return ext in SUPPORTED_EXTS


def _trim(text: str) -> tuple[str, bool]:
    clean = " ".join((text or "").split())
    if not clean:
        raise AsrError("未识别到语音内容，请重试", 422)
    if len(clean) > MAX_TRANSCRIPT_CHARS:
        return clean[:MAX_TRANSCRIPT_CHARS], True
    return clean, False


def _transcribe_mock() -> str:
    return settings.asr_mock_text or ""


def _audio_filename(mime: str) -> str:
    """按 MIME 生成固定文件名，避免把用户文件名拼进上游请求头。"""
    mapping = {
        "audio/webm": "voice.webm",
        "audio/ogg": "voice.ogg",
        "audio/wav": "voice.wav",
        "audio/x-wav": "voice.wav",
        "audio/wave": "voice.wav",
        "audio/mpeg": "voice.mp3",
        "audio/mp4": "voice.m4a",
        "audio/aac": "voice.aac",
        "audio/flac": "voice.flac",
    }
    return mapping.get(mime, "voice.webm")


def _transcribe_openai(audio: bytes, mime: str) -> str:
    base = settings.asr_base_url.rstrip("/")
    if not base:
        raise AsrError("语音识别服务未配置（ASR_BASE_URL 为空）", 503)
    # 拒绝把密钥发到非 http(s) 地址；同时避免 file:// 之类的意外协议
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AsrError("语音识别服务地址不合法", 503)

    data = {"model": settings.asr_model, "response_format": "json"}
    if settings.asr_language:
        data["language"] = settings.asr_language
    headers = {"Authorization": f"Bearer {settings.asr_api_key.strip()}"}

    try:
        with httpx.Client(timeout=settings.asr_timeout) as client:
            resp = client.post(
                f"{base}/audio/transcriptions",
                headers=headers,
                data=data,
                files={"file": (_audio_filename(mime), audio, mime or "audio/webm")},
            )
    except httpx.TimeoutException as exc:
        logger.warning("ASR 超时：%.1fs", settings.asr_timeout)
        raise AsrError("语音识别超时，请缩短录音后重试", 504) from exc
    except httpx.HTTPError as exc:
        logger.warning("ASR 请求失败：%s", type(exc).__name__)
        raise AsrError("语音识别服务暂不可用，请稍后再试", 502) from exc

    if resp.status_code >= 400:
        logger.warning("ASR 上游返回 %s：%s", resp.status_code, resp.text[:200])
        raise AsrError("语音识别服务返回错误，请稍后再试", 502)

    try:
        payload = resp.json()
        text = payload.get("text") if isinstance(payload, dict) else None
    except ValueError as exc:
        raise AsrError("语音识别服务返回格式错误", 502) from exc
    if not isinstance(text, str):
        raise AsrError("语音识别服务返回格式错误", 502)
    return text


def transcribe(audio: bytes, filename: str | None, content_type: str | None) -> Transcript:
    """把音频转成文字；调用方负责大小与格式校验。"""
    if not ready():
        raise AsrError("语音识别未启用或未配置 API Key", 503)
    if not audio:
        raise AsrError("音频为空，请重新录音", 400)

    started = time.monotonic()
    mime = normalize_mime(content_type)
    if is_mock():
        raw = _transcribe_mock()
    else:
        raw = _transcribe_openai(audio, mime)

    text, truncated = _trim(raw)
    elapsed = int((time.monotonic() - started) * 1000)
    logger.info(
        "ASR 完成：provider=%s bytes=%d chars=%d truncated=%s elapsed=%dms",
        settings.asr_provider,
        len(audio),
        len(text),
        truncated,
        elapsed,
    )
    return Transcript(
        text=text,
        provider=settings.asr_provider,
        truncated=truncated,
        elapsed_ms=elapsed,
    )
