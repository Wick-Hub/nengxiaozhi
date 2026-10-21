"""语音识别接口（任务 4.7）：状态 + 音频转写。

前端录音后以 ``multipart/form-data`` 上传 ``file`` 字段，拿到文字再填入输入框，
随后的问答仍走 ``POST /api/chat/stream``——与文字输入是同一条链路。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.models.schemas import ApiResult
from app.security.ratelimit import rate_limit
from app.services import asr

router = APIRouter(dependencies=[Depends(rate_limit("asr"))])


@router.get("/status", response_model=ApiResult, summary="语音识别能力状态")
def asr_status() -> ApiResult:
    return ApiResult(ok=True, data=asr.status())


@router.post("/transcribe", response_model=ApiResult, summary="语音转文字")
async def transcribe(file: UploadFile = File(...)) -> ApiResult:
    if not asr.ready():
        raise HTTPException(status_code=503, detail="语音识别未启用或未配置 API Key")
    if not asr.is_supported(file.filename, file.content_type):
        raise HTTPException(
            status_code=415,
            detail="不支持的音频格式，请上传 webm/ogg/wav/mp3/m4a 等音频",
        )

    limit = max(1, int(settings.asr_max_bytes))
    audio = await file.read(limit + 1)
    if len(audio) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"音频超过大小限制（{limit // (1024 * 1024) or 1} MB）",
        )

    try:
        result = await run_in_threadpool(
            asr.transcribe, audio, file.filename, file.content_type
        )
    except asr.AsrError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return ApiResult(
        ok=True,
        data={
            "text": result.text,
            "provider": result.provider,
            "truncated": result.truncated,
            "elapsedMs": result.elapsed_ms,
        },
    )
