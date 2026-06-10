"""Dify 智能体服务。

与 ``CozeService`` 同形接口,方便 ``WeChatService.process_single_message``
无差别地调用。WeChat 场景下:
- ``upload_file(content, file_name)`` → Dify 返回的 upload_file_id (UUID)
- ``run_workflow(input_data, user_id)`` → 把 ``input_data`` 里的
  ``file_image_id`` / ``file_voice_id`` 字段值理解为"已上传的 Dify file UUID",
  转成 Dify 工作流的 file-array 输入格式,然后调用 workflow,
  把响应归一化成 ``{"content": <reply_text>, "raw": <raw>}`` 形态(便于上游
  wechat_service 既有的 ``content`` / ``data`` 解析逻辑直接复用)。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx

from app.core.config import settings
from app.core.exceptions import AIBackendError  # 通用 AI 后端异常
from app.services.dify_client import DifyClient, DifyError
from app.services.response_parser import extract_assistant_text

logger = logging.getLogger(__name__)


def _guess_audio_mime(filename: str) -> str:
    """Normalize WeChat 语音 MIME。Dify 接受 wav/mp3/m4a/webm/amr。"""
    name = (filename or "").lower()
    if name.endswith(".wav"):
        return "audio/wav"
    if name.endswith(".mp3"):
        return "audio/mpeg"
    if name.endswith(".m4a"):
        return "audio/mp4"
    if name.endswith(".webm"):
        return "audio/webm"
    if name.endswith(".amr"):
        return "audio/amr"
    if name.endswith(".ogg") or name.endswith(".oga"):
        return "audio/ogg"
    return "application/octet-stream"


def _guess_image_mime(filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".webp"):
        return "image/webp"
    if name.endswith(".gif"):
        return "image/gif"
    # jpg / jpeg / 默认
    return "image/jpeg"


class DifyService:
    """Dify 智能体服务 (workflow 类型)。"""

    def __init__(self, end_user: Optional[str] = None) -> None:
        self._client = DifyClient(
            api_base=settings.dify.api_base,
            api_key=settings.dify.api_key.get_secret_value(),
            end_user=end_user or settings.dify.end_user_default,
            upload_timeout=float(settings.dify.upload_timeout),
            workflow_timeout=float(settings.dify.workflow_timeout),
        )
        # 保持一个长连接的 httpx 客户端,以便 close() 时统一关闭
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(float(settings.dify.workflow_timeout))
        )

    @property
    def client(self) -> DifyClient:
        return self._client

    # ------------------------------------------------------------------
    # 与 CozeService 同形的对外接口
    # ------------------------------------------------------------------
    async def upload_file(self, file_content: bytes, file_name: str) -> str:
        """上传文件到 Dify 并返回 upload_file_id (UUID)。

        Args:
            file_content: 文件二进制内容
            file_name:    文件名 (用于推断 content_type)

        Returns:
            Dify 文件 UUID
        """
        # 推断 content_type
        if any(file_name.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
            ctype = _guess_image_mime(file_name)
        else:
            ctype = _guess_audio_mime(file_name)

        try:
            return await self._client.upload_file(
                filename=file_name,
                content=file_content,
                content_type=ctype,
            )
        except DifyError as e:
            raise AIBackendError(f"Dify 文件上传失败: {e}") from e

    async def run_workflow(self, input_data: Any, user_id: str = "wechat_user") -> Dict[str, Any]:
        """触发 Dify workflow。

        Args:
            input_data: 简化输入,支持两种形态:
                1) ``{"text": str, "file_image_id": str, "file_voice_id": str}``
                   (与 CozeService 一致,file_*_id 字段值是已通过本服务
                   ``upload_file`` 拿到的 Dify UUID)
                2) 已是 Coze workflow 的 parameters 字典
            user_id: 调用方传入的用户标识(WeChat 场景为 external_userid),
                     会被用作 Dify 的 ``end_user`` 字段。

        Returns:
            形如 ``{"content": <reply_text>, "raw": <raw_dify_body>}`` 的字典。
            ``content`` 字段供 ``WeChatService`` 既有的解析逻辑直接读取;
            ``raw`` 字段保留完整响应,便于调试。
        """
        end_user = user_id or settings.dify.end_user_default

        # 若 DifyClient 缓存了默认 end_user 与本次不同,临时构造新 client
        client = self._client
        if client.end_user != end_user:
            client = DifyClient(
                api_base=client.api_base,
                api_key=client.api_key,
                end_user=end_user,
                upload_timeout=client.upload_timeout,
                workflow_timeout=client.workflow_timeout,
            )

        # 构造 workflow inputs
        inputs: Dict[str, Any] = {}
        if isinstance(input_data, dict):
            if any(k in input_data for k in ("text", "file_image_id", "file_voice_id")):
                if input_data.get("text"):
                    inputs[settings.dify.input_text] = input_data["text"]
                if input_data.get("file_image_id"):
                    # Dify 文件型输入必须是数组,即使只有一个文件
                    inputs[settings.dify.input_image] = [
                        client.file_ref(str(input_data["file_image_id"]), "image")
                    ]
                if input_data.get("file_voice_id"):
                    inputs[settings.dify.input_audio] = [
                        client.file_ref(str(input_data["file_voice_id"]), "audio")
                    ]
            else:
                # 透传 (假定调用方已是 Dify inputs 形态)
                inputs = dict(input_data)
        else:
            # 兜底:转字符串塞到 text 输入
            inputs[settings.dify.input_text] = str(input_data) if input_data is not None else ""

        # 兜底:无任何有效字段时,塞默认文本
        if not inputs:
            inputs[settings.dify.input_text] = "收到您的消息"

        logger.info("Dify workflow inputs keys=%s", list(inputs.keys()))

        # 调用 workflow
        try:
            raw = await client.run_workflow(inputs=inputs, response_mode="blocking")
        except DifyError as e:
            logger.error("Dify workflow error: %s", e)
            raise AIBackendError(f"Dify workflow 失败: {e}") from e

        assistant_text = extract_assistant_text(raw, preferred_key=settings.dify.output_text)
        logger.info(
            "Dify workflow 成功: assistant_text_len=%d",
            len(assistant_text) if assistant_text else 0,
        )

        # 归一化成 Coze-like 形态,让 WeChatService 既有的解析逻辑 (content / data 字段)
        # 无差别工作。
        return {
            "content": assistant_text,
            "content_type": "text",
            "node_type": "dify_workflow",
            "raw": raw,
        }

    async def close(self) -> None:
        try:
            await self._http.aclose()
        except Exception as e:
            logger.warning(f"DifyService http client close 失败: {e}")
