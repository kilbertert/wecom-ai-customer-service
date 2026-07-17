"""通义 paraformer ASR: WAV 文件 -> 转录文本。

wecom 侧把用户语音转文本作为 query 发送 Dify (Dify chatflow 无 ASR 节点,
speech_to_text feature 只管前端 UI, 见 multimodal-vision-findings 记忆)。

用 dashscope SDK 的 Recognition (paraformer-realtime-v2, 本地 wav 流式识别)。
同步 call 在线程池跑 (wecom 是 async)。
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import dashscope
from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

from app.core.config import settings

logger = logging.getLogger(__name__)

# ASR 是同步阻塞 (WebSocket 流式), 用独立线程池避免阻塞事件循环
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="asr")


class _ASRCallback(RecognitionCallback):
    """收集 paraformer 流式识别的最终文本。"""

    def __init__(self) -> None:
        self.text: str = ""
        self.error: Optional[str] = None

    def on_open(self) -> None:
        pass

    def on_close(self) -> None:
        pass

    def on_complete(self) -> None:
        pass

    def on_error(self, result) -> None:
        self.error = str(result)

    def on_event(self, result: RecognitionResult) -> None:
        # paraformer 流式: 每个 sentence event 携带当前累积文本,
        # 最后一个 sentence-end 是完整文本。持续覆盖取最新。
        sent = result.get_sentence()
        if sent is not None:
            t = getattr(sent, "text", None)
            if t:
                self.text = t


def _transcribe_sync(wav_path: str) -> str:
    api_key = settings.asr.dashscope_api_key.get_secret_value()
    if not api_key:
        raise RuntimeError("ASR_DASHSCOPE_API_KEY 未配置")
    dashscope.api_key = api_key
    cb = _ASRCallback()
    recognition = Recognition(
        model=settings.asr.model,
        callback=cb,
        format="wav",
        sample_rate=settings.asr.sample_rate,
    )
    result = recognition.call(wav_path)
    if cb.error:
        raise RuntimeError(f"paraformer ASR error: {cb.error}")
    # Recognition.call 返回 RecognitionResult, 最终文本在
    # result.output['sentence'][*]['text'] (流式 on_event 回调在 call 期间
    # 可能不触发, 以返回值为准 - 实测 paraformer-realtime-v2)。
    text = cb.text  # 回调兜底
    out = getattr(result, "output", None)
    if isinstance(out, dict):
        sentences = out.get("sentence") or []
        joined = "".join(
            s.get("text", "") for s in sentences if isinstance(s, dict)
        )
        if joined.strip():
            text = joined
    return text.strip()


async def transcribe(wav_path: str) -> str:
    """异步转写 WAV 文件 -> 文本。

    Args:
        wav_path: 本地 WAV 文件路径 (16kHz, mono)

    Returns:
        转录文本。未启用/失败时返回空串 (调用方兜底占位)。
    """
    if not settings.asr.enabled:
        logger.debug("ASR 未启用, 跳过转写")
        return ""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, _transcribe_sync, wav_path)
    except Exception as e:
        logger.error("ASR 转写失败 wav=%s: %s", wav_path, e, exc_info=True)
        return ""


__all__ = ["transcribe"]
