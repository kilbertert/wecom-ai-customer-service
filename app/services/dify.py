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
from app.services.multimodal import _coerce_url_list
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


def _normalize_retriever_resources(resources: list) -> list:
    """Chatflow ``metadata.retriever_resources`` → workflow-style chunks。

    Chatflow 形态 (Dify 官方):
        ``{position, dataset_id, dataset_name, document_id, document_name,
           segment_id, score, content}``

    归一为 ``format_knowledge_lines`` 期望的形态:
        ``{title, content, metadata: {score, segment_word_count}}``

    让现有 trace 格式化器无需感知 chatflow / workflow 差异。
    """
    if not isinstance(resources, list):
        return []
    out = []
    for r in resources:
        if not isinstance(r, dict):
            continue
        content = str(r.get("content") or "")
        title = (
            r.get("document_name")
            or r.get("dataset_name")
            or f"chunk-{r.get('position', '?')}"
        )
        out.append(
            {
                "title": title,
                "content": content,
                "metadata": {
                    "score": r.get("score"),
                    "segment_word_count": len(content),
                    "document_id": r.get("document_id"),
                    "segment_id": r.get("segment_id"),
                },
            }
        )
    return out


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
    """Dify 智能体服务 (支持 workflow / chatflow 两种 app 类型)。

    通过 ``settings.dify.app_mode`` 切换:
        - ``"workflow"``: 旧 Workflow app (/v1/workflows/run), outputs 在 data.outputs
        - ``"chatflow"``: 新 Chatflow / advanced-chat app (/v1/chat-messages),
                          answer 在顶层, 知识库在 metadata.retriever_resources
    """

    def __init__(self, end_user: Optional[str] = None) -> None:
        app_mode = getattr(settings.dify, "app_mode", "chatflow") or "chatflow"
        self._client = DifyClient(
            api_base=settings.dify.api_base,
            api_key=settings.dify.api_key.get_secret_value(),
            end_user=end_user or settings.dify.end_user_default,
            upload_timeout=float(settings.dify.upload_timeout),
            workflow_timeout=float(settings.dify.workflow_timeout),
            chatflow_timeout=float(
                getattr(settings.dify, "chatflow_timeout", 120) or 120
            ),
            app_mode=app_mode,
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
        if any(
            file_name.lower().endswith(ext)
            for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")
        ):
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

    async def _run_chatflow(
        self,
        input_data: Any,
        client: "DifyClient",
    ) -> Dict[str, Any]:
        """Chatflow (advanced-chat) app 调用路径。

        与 workflow 的关键差异:
            - 用户文本走 ``query`` 字段, 不放在 ``inputs`` 里
            - 文件走顶层 ``files`` 数组, 不放在 ``inputs[<file_var>]`` 里
            - 响应扁平: ``answer`` + ``metadata.retriever_resources``,
              没有 ``data.outputs`` 嵌套

        响应归一化: 把 chatflow 的扁平响应写到 ``raw.data.outputs.text``
        和 ``raw.data.outputs.knowledge``, 让现有 ``extract_assistant_text`` /
        ``format_knowledge_lines`` 无需改动即可消费。
        """
        # 1) 提取 query (用户文本) 与 files (媒体)
        query = ""
        files: List[Dict[str, Any]] = []
        if isinstance(input_data, dict):
            query = str(input_data.get("text") or "").strip()

            # image: 优先 url 模式, 兼容 id 模式
            if input_data.get("file_image_url"):
                files.append(
                    {
                        "type": "image",
                        "transfer_method": "remote_url",
                        "url": input_data["file_image_url"],
                    }
                )
            elif input_data.get("file_image_id"):
                files.append(
                    {
                        "type": "image",
                        "transfer_method": "local_file",
                        "upload_file_id": str(input_data["file_image_id"]),
                    }
                )

            # voice: 仅 local_file 模式 (chatflow 不支持 voice remote_url)
            if input_data.get("file_voice_id"):
                files.append(
                    {
                        "type": "audio",
                        "transfer_method": "local_file",
                        "upload_file_id": str(input_data["file_voice_id"]),
                    }
                )
        else:
            query = str(input_data) if input_data is not None else ""

        # 兜底: 无文本时给个默认值, 防止 query 为空被 Dify 拒
        if not query:
            query = "收到您的消息"

        # 2) 调 chatflow
        try:
            raw = await client.run_chatflow(
                query=query,
                inputs={},
                files=files or None,
                response_mode="blocking",
            )
        except DifyError as e:
            logger.error("Dify chatflow error: %s", e)
            raise AIBackendError(f"Dify chatflow 失败: {e}") from e

        # 3) 提取 answer + 归一化
        answer = (raw or {}).get("answer") or ""
        metadata = (raw or {}).get("metadata") or {}
        retriever_resources = metadata.get("retriever_resources") or []

        # 归一化 retriever_resources → workflow-style chunks
        knowledge_chunks = _normalize_retriever_resources(retriever_resources)

        # 归一化整个响应到 workflow 形态, 让下游 extract_assistant_text /
        # format_knowledge_lines 不用改
        normalized_raw = {
            "task_id": raw.get("task_id"),
            "workflow_run_id": raw.get("workflow_run_id") or raw.get("message_id"),
            "conversation_id": raw.get("conversation_id"),
            "mode": raw.get("mode", "advanced-chat"),
            "data": {
                "id": raw.get("id"),
                "workflow_id": "",
                "status": "succeeded" if (raw.get("event") == "message") else "failed",
                "outputs": {
                    "text": answer,  # 让 extract_assistant_text 直接命中
                    "answer": answer,
                    "knowledge": knowledge_chunks,
                },
                "error": None,
            },
            "metadata": metadata,
        }

        logger.info(
            "Dify chatflow 成功: answer_len=%d, knowledge_chunks=%d",
            len(answer),
            len(knowledge_chunks),
        )

        return {
            "content": answer,
            "content_type": "text",
            "node_type": "dify_chatflow",
            "text": answer,
            "images": [],
            "videos": [],
            "files": [],
            "raw": normalized_raw,
        }

    async def run_workflow(
        self, input_data: Any, user_id: str = "wechat_user"
    ) -> Dict[str, Any]:
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
                chatflow_timeout=client.chatflow_timeout,
                app_mode=client.app_mode,
            )

        # 路由: chatflow / advanced-chat app 用 run_chatflow
        if (client.app_mode or "chatflow") == "chatflow":
            return await self._run_chatflow(input_data, client)

        # 构造 workflow inputs
        inputs: Dict[str, Any] = {}
        if isinstance(input_data, dict):
            if any(
                k in input_data
                for k in (
                    "text",
                    "file_image_id",
                    "file_image_url",
                    "file_voice_id",
                    "file_voice_url",
                )
            ):
                # 一期改造 (2026-06): 透传所有非 file_* 字段, 让群聊上下文 (chat_id, recent_context 等)
                # 也能进 Dify workflow inputs (方案 B 群聊)
                _NON_FILE_KEYS = {
                    "text",  # 文本字段, 用 settings.dify.input_text 变量名落地
                    "user_id",
                    "chat_id",
                    "is_group_chat",
                    "recent_context",
                    "current_sender",
                    "should_judge",
                    "language",
                    "session_id",
                    "turn",
                    "hint_endpoint",
                    "hint_region",
                }
                for k, v in input_data.items():
                    if k in _NON_FILE_KEYS:
                        # 文本字段: 用 DifySettings 配的 input_text 变量名
                        if k == "text":
                            inputs[settings.dify.input_text] = v
                        else:
                            inputs[k] = v

                # 1) file_image: 优先 url 模式, 兼容 id 模式
                if input_data.get("file_image_url"):
                    inputs[settings.dify.input_image] = [
                        {
                            "type": "image",
                            "transfer_method": "remote_url",
                            "url": input_data["file_image_url"],
                        }
                    ]
                elif input_data.get("file_image_id"):
                    inputs[settings.dify.input_image] = [
                        client.file_ref(str(input_data["file_image_id"]), "image")
                    ]
                # 2) file_voice: 同上
                if input_data.get("file_voice_url"):
                    inputs[settings.dify.input_audio] = [
                        {
                            "type": "audio",
                            "transfer_method": "remote_url",
                            "url": input_data["file_voice_url"],
                        }
                    ]
                elif input_data.get("file_voice_id"):
                    inputs[settings.dify.input_audio] = [
                        client.file_ref(str(input_data["file_voice_id"]), "audio")
                    ]
            else:
                # 透传 (假定调用方已是 Dify inputs 形态)
                inputs = dict(input_data)
        else:
            # 兜底:转字符串塞到 text 输入
            inputs[settings.dify.input_text] = (
                str(input_data) if input_data is not None else ""
            )

        # 兜底:无任何有效字段时,塞默认文本
        if not inputs:
            inputs[settings.dify.input_text] = "收到您的消息"

        logger.info("Dify workflow inputs keys=%s", list(inputs.keys()))
        # DEBUG: dump 实际发送给 Dify 的 inputs (诊断 input_img_id 格式)
        import json as _json

        logger.warning(
            f"[DIAG] inputs payload: {_json.dumps(inputs, ensure_ascii=False)[:500]}"
        )

        # 调用 workflow
        try:
            raw = await client.run_workflow(inputs=inputs, response_mode="blocking")
        except DifyError as e:
            logger.error("Dify workflow error: %s", e)
            raise AIBackendError(f"Dify workflow 失败: {e}") from e

        assistant_text = extract_assistant_text(
            raw, preferred_key=settings.dify.output_text
        )
        logger.info(
            "Dify workflow 成功: assistant_text_len=%d",
            len(assistant_text) if assistant_text else 0,
        )
        # 一期诊断: dump raw 头尾到日志, 看真实结构
        try:
            raw_str = json.dumps(raw, ensure_ascii=False)
            logger.warning(
                "DIAG raw head (300): %s",
                raw_str[:300].replace("\n", " "),
            )
            logger.warning(
                "DIAG raw tail (300): %s",
                raw_str[-300:].replace("\n", " "),
            )
            # 检查 output 字段的真实形态
            outputs_check = ((raw or {}).get("data") or {}).get("outputs") or {}
            for k, v in outputs_check.items():
                v_str = str(v)
                logger.warning(
                    "DIAG outputs[%s] type=%s len=%d head=%.120s",
                    k,
                    type(v).__name__,
                    len(v_str),
                    v_str[:120].replace("\n", " "),
                )
        except Exception as diag_err:
            logger.error(f"DIAG 失败: {diag_err}")

        # 一期新增: 从 Dify 工作流 outputs 节点提取多模态字段。
        # Dify 工作流结束节点声明 images/videos/files (Array[String]) 后,
        # 响应 raw["data"]["outputs"]["images"] 等就是 URL 数组。
        outputs = ((raw or {}).get("data") or {}).get("outputs") or {}
        images = _coerce_url_list(outputs.get("images"))
        videos = _coerce_url_list(outputs.get("videos"))
        files = _coerce_url_list(outputs.get("files"))
        if images or videos or files:
            logger.info(
                "Dify 多模态字段: images=%d, videos=%d, files=%d",
                len(images),
                len(videos),
                len(files),
            )

        # 归一化成 Coze-like 形态,让 WeChatService 既有的解析逻辑 (content / data 字段)
        # 无差别工作。同时携带顶层多模态字段,供 compose_multimodal_markdown 使用。
        return {
            "content": assistant_text,
            "content_type": "text",
            "node_type": "dify_workflow",
            # 一期新增: 顶层多模态字段
            "text": assistant_text,
            "images": images,
            "videos": videos,
            "files": files,
            "raw": raw,
        }

    async def close(self) -> None:
        try:
            await self._http.aclose()
        except Exception as e:
            logger.warning(f"DifyService http client close 失败: {e}")
