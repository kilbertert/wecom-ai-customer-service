"""Dify 智能体服务。

对外接口 (``upload_file`` / ``run_workflow``) 供 ``MessageProcessor`` 调用。
WeChat 场景下:
- ``upload_file(content, file_name)`` → Dify 返回的 upload_file_id (UUID)
- ``run_workflow(input_data, user_id)`` → 把 ``input_data`` 里的
  ``file_image_bytes`` 字段值理解为"原始图片字节" (app 无关, 上传在发送点 _run_chatflow 按 app 进行),
  转成 Dify 工作流的 file-array 输入格式,然后调用 workflow,
  把响应归一化成 ``{"content": <reply_text>, "raw": <raw>}`` 形态(便于上游
  wechat_service 既有的 ``content`` / ``data`` 解析逻辑直接复用)。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.config import settings
from app.core.exceptions import AIBackendError  # 通用 AI 后端异常
from app.services.dify_client import DifyClient, DifyError
from app.services.multimodal import _coerce_url_list
from app.services.response_parser import extract_assistant_text

logger = logging.getLogger(__name__)


# ======================================================================
# 上传文件字节缓存 (bug 截图入飞书附件用)
# ======================================================================
# WeCom 后端 workers=1, 进程内缓存可靠。上传 Dify 时顺手缓存原始字节,
# 后续 /add 写飞书附件时按 upload_file_id 回取, 避免再向 Dify 下载
# (Dify 文件下载有 app token 归属 A/B + end_user 所有权不确定性)。
# 缓存 miss (超时/跨进程) 时 fetch_upload_bytes 兜底走 Dify download。
_UPLOAD_CACHE: Dict[str, Tuple[bytes, str, str, float]] = {}
_UPLOAD_CACHE_TTL = 1800.0  # 30 分钟 (bug 多轮确认流程通常数分钟内完成)
_UPLOAD_CACHE_CAP = 100


def _cache_put(upload_id: str, content: bytes, filename: str, content_type: str) -> None:
    """缓存上传文件字节 (带 TTL)。超容量时先淘汰过期项。"""
    now = time.time()
    if len(_UPLOAD_CACHE) >= _UPLOAD_CACHE_CAP:
        for k in [k for k, v in _UPLOAD_CACHE.items() if v[3] < now]:
            _UPLOAD_CACHE.pop(k, None)
    _UPLOAD_CACHE[upload_id] = (content, filename, content_type, now + _UPLOAD_CACHE_TTL)


def _cache_get(upload_id: str) -> Optional[Tuple[bytes, str, str]]:
    """取缓存 (过期返回 None 并清理)。返回 (bytes, filename, content_type)。"""
    v = _UPLOAD_CACHE.get(upload_id)
    if not v:
        return None
    if v[3] < time.time():
        _UPLOAD_CACHE.pop(upload_id, None)
        return None
    return (v[0], v[1], v[2])


async def fetch_upload_bytes(upload_file_id: str) -> Optional[Tuple[bytes, str, str]]:
    """按 Dify upload_file_id 取文件字节, 供飞书附件上传用。

    优先进程内缓存 (上传 Dify 时已存); miss 则从 Dify 文件库下载兜底
    (先 app B token, 404/403 回退 app A, 因 KF 图片默认上传 A、bug 路由切 B)。

    Returns:
        (bytes, filename, content_type) 或 None (全部失败)
    """
    uid = (upload_file_id or "").strip()
    if not uid:
        return None
    cached = _cache_get(uid)
    if cached:
        return cached

    # 兜底: 从 Dify 文件库下载 (无缓存或缓存过期)
    base = settings.dify.api_base
    eu = settings.dify.end_user_default
    keys: List[str] = []
    try:
        kb = settings.dify.api_key_b.get_secret_value() if settings.dify.api_key_b else ""
    except Exception:
        kb = ""
    try:
        ka = settings.dify.api_key_a.get_secret_value() or settings.dify.api_key.get_secret_value()
    except Exception:
        ka = ""
    if kb:
        keys.append(kb)
    if ka and ka not in keys:
        keys.append(ka)

    for key in keys:
        try:
            dc = DifyClient(api_base=base, api_key=key, end_user=eu)
            content = await dc.download_file(file_id=uid)
            logger.info("[dify] 回取文件 %s 成功 (兜底下载) size=%dB", uid, len(content))
            return (content, f"{uid}.jpg", "image/jpeg")
        except DifyError as e:
            logger.warning("[dify] 回取文件 %s 失败 (key 末尾 %s): %s", uid, key[-6:] if key else "?", e)
            continue
    logger.warning("[dify] 回取文件 %s 全部失败 (缓存 miss + 下载失败)", uid)
    return None


# ======================================================================
# 会话 -> 图片 缓存 (bug 截图跨轮: turn1 发图, turn2 写表时按 conv_id 回取)
# ======================================================================
# bug 多轮流程: turn1 用户发截图+描述 -> 分类 bug -> 引导确认; turn2 确认 ->
# 6260a 写飞书。图片 file_id 在 turn1, 写表在 turn2, 需跨轮保留。Dify 6260a
# 在 /add body 里传 conversation_id, 后端按 conv_id 取本缓存得 file_id 列表,
# 再 fetch_upload_bytes 取字节 -> 上传飞书附件。workers=1 进程内可靠。
_CONV_IMAGE_CACHE: Dict[str, Tuple[List[str], float]] = {}
_CONV_IMAGE_TTL = 3600.0  # 1h (bug 多轮流程通常数分钟内完成)
_CONV_IMAGE_CAP = 200


def _conv_image_put(conv_id: str, file_id: str) -> None:
    """追加一个图片 file_id 到会话的图片列表 (带 TTL, 去重, 不可变)。"""
    now = time.time()
    if len(_CONV_IMAGE_CACHE) >= _CONV_IMAGE_CAP:
        for k in [k for k, v in _CONV_IMAGE_CACHE.items() if v[1] < now]:
            _CONV_IMAGE_CACHE.pop(k, None)
    ids, _ = _CONV_IMAGE_CACHE.get(conv_id, ([], 0.0))
    if file_id and file_id not in ids:
        ids = ids + [file_id]  # 新列表, 不就地改
    _CONV_IMAGE_CACHE[conv_id] = (ids, now + _CONV_IMAGE_TTL)


def _conv_image_get(conv_id: str) -> List[str]:
    """取会话累积的图片 file_id 列表 (过期返回 [])。"""
    v = _CONV_IMAGE_CACHE.get(conv_id)
    if not v:
        return []
    if v[1] < time.time():
        _CONV_IMAGE_CACHE.pop(conv_id, None)
        return []
    return list(v[0])


def _conv_image_clear(conv_id: str) -> None:
    """清空会话图片缓存 (写表后调用, 防同会话下个 bug 复用旧图)。"""
    _CONV_IMAGE_CACHE.pop(conv_id, None)


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


def _detect_image_mime(content: bytes, filename: str = "") -> str:
    """按 magic bytes 检测图片 MIME, 回退到文件名扩展名。

    避免 content_type 与实际内容不匹配 (企微图可能是 png/webp, 固定 jpeg 会
    Dify "Invalid upload file")。集中在上传点检测, 保证发送给 Dify 的类型正确。
    """
    if content.startswith(b"\x89PNG"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"GIF8"):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return _guess_image_mime(filename)


class DifyService:
    """Dify 智能体服务 (支持 workflow / chatflow 两种 app 类型)。

    通过 ``settings.dify.app_mode`` 切换:
        - ``"workflow"``: 旧 Workflow app (/v1/workflows/run), outputs 在 data.outputs
        - ``"chatflow"``: 新 Chatflow / advanced-chat app (/v1/chat-messages),
                          answer 在顶层, 知识库在 metadata.retriever_resources
    """

    def __init__(self, end_user: Optional[str] = None) -> None:
        app_mode = getattr(settings.dify, "app_mode", "chatflow") or "chatflow"
        eu = end_user or settings.dify.end_user_default

        def _make_client(api_key: str) -> DifyClient:
            return DifyClient(
                api_base=settings.dify.api_base,
                api_key=api_key,
                end_user=eu,
                upload_timeout=float(settings.dify.upload_timeout),
                workflow_timeout=float(settings.dify.workflow_timeout),
                chatflow_timeout=float(
                    getattr(settings.dify, "chatflow_timeout", 120) or 120
                ),
                app_mode=app_mode,
            )

        # 双 app: A=KB问答, B=bug追踪。key_a 回退 api_key (单 app 兼容); key_b 空=单 app 模式
        key_a = (settings.dify.api_key_a.get_secret_value()
                 or settings.dify.api_key.get_secret_value())
        key_b = settings.dify.api_key_b.get_secret_value()
        self._clients: Dict[str, DifyClient] = {"A": _make_client(key_a)}
        self._dual_app = bool(key_b)
        if self._dual_app:
            self._clients["B"] = _make_client(key_b)
        # 兼容: self._client / self.client 指向 A
        self._client = self._clients["A"]
        # 保持一个长连接的 httpx 客户端,以便 close() 时统一关闭
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(float(settings.dify.workflow_timeout))
        )

    @property
    def dual_app(self) -> bool:
        """是否双 app 模式 (api_key_b 已配置)。"""
        return self._dual_app

    def _client_for_app(self, app: str) -> DifyClient:
        """按 app 标识取对应 DifyClient; 单 app 模式或未知 app 一律回退 A。"""
        if self._dual_app and app == "B":
            return self._clients["B"]
        return self._clients["A"]


    @property
    def client(self) -> DifyClient:
        return self._client

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    async def upload_file(self, file_content: bytes, file_name: str, user_id: str = "", app: str = "A") -> str:
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
            client = self._client_for_app(app) if hasattr(self, "_client_for_app") else self._client
            # Dify access_controller requires_user_ownership: created_by 必须 == chatflow user_id
            if user_id and getattr(client, "end_user", "") != user_id:
                from app.services.dify_client import DifyClient
                client = DifyClient(
                    api_base=client.api_base, api_key=client.api_key, end_user=user_id,
                    upload_timeout=client.upload_timeout, workflow_timeout=client.workflow_timeout,
                    chatflow_timeout=client.chatflow_timeout, app_mode=client.app_mode,
                )
            file_id = await client.upload_file(
                filename=file_name,
                content=file_content,
                content_type=ctype,
            )
            # 缓存原始字节, 供 bug 截图入飞书附件回取 (workers=1 进程内可靠)
            _cache_put(str(file_id), file_content, file_name, ctype or "application/octet-stream")
            return file_id
        except DifyError as e:
            raise AIBackendError(f"Dify 文件上传失败: {e}") from e

    async def _run_chatflow(
        self,
        input_data: Any,
        client: "DifyClient",
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Chatflow (advanced-chat) app 调用路径。

        与 workflow 的关键差异:
            - 用户文本走 ``query`` 字段, 不放在 ``inputs`` 里
            - 文件走顶层 ``files`` 数组, 不放在 ``inputs[<file_var>]`` 里
            - 响应扁平: ``answer`` + ``metadata.retriever_resources``,
              没有 ``data.outputs`` 嵌套

        多轮续接:
            - 传入上次会话的 ``conversation_id`` (首次为 None/空串 → Dify 新建会话)
            - 响应里的 ``conversation_id`` 通过返回 dict 顶层字段回传, 供
              ``ConversationStore.save`` 持久化, 下一轮续接。

        响应归一化: 把 chatflow 的扁平响应写到 ``raw.data.outputs.text``
        和 ``raw.data.outputs.knowledge``, 让现有 ``extract_assistant_text`` /
        ``format_knowledge_lines`` 无需改动即可消费。
        """
        # 1) 提取 query (用户文本) 与 files (媒体)
        # 图片上传在【发送点】进行: input_data 携带 app 无关的原始字节, 在此按目标
        # client(正确 app + end_user)上传。Dify 文件库按 app 隔离, 只有发送时目标
        # app 才确定, 在此上传才能保证跨 app 改投(A->B)时文件归属正确, 根除
        # "A 的 file_id 发给 B 报 Invalid upload file"。
        query = ""
        files: List[Dict[str, Any]] = []
        upload_file_id = ""  # 本轮上传的图片 file_id(目标 app 绑定), 供 conv 缓存
        if isinstance(input_data, dict):
            query = str(input_data.get("text") or "").strip()

            img_bytes = input_data.get("file_image_bytes")
            if img_bytes:
                img_name = input_data.get("file_image_name") or "image.png"
                ctype = _detect_image_mime(img_bytes, img_name)
                upload_file_id = str(await client.upload_file(
                    filename=img_name, content=img_bytes, content_type=ctype,
                ))
                # 缓存字节: 供 bug 截图入飞书附件回取 + 改投重传兜底
                _cache_put(upload_file_id, img_bytes, img_name, ctype)
                files.append(
                    {
                        "type": "image",
                        "transfer_method": "local_file",
                        "upload_file_id": upload_file_id,
                    }
                )
                logger.info(
                    "[DIFY] 图片上传至 app(end_user=%s) file_id=%s size=%dB",
                    client.end_user, upload_file_id, len(img_bytes),
                )
        else:
            query = str(input_data) if input_data is not None else ""

        # 兜底: 无文本时给个默认值, 防止 query 为空被 Dify 拒
        if not query:
            query = "收到您的消息"

        # F3) chatflow user_input_form select 字段 (input_language /
        # input_hint_endpoint / input_hint_region) 决定 chatflow 内 L1 板块路由。
        # 旧版恒传 inputs={} → Dify 全用字段 default="" → 路由精度受损。
        # 取值优先级: input_data 透传 (language/hint_endpoint/hint_region) > 部署级
        # 配置 (DIFY_CHATFLOW_INPUT_*) > 不传 (Dify 用 default)。仅传非空值。
        inputs: Dict[str, Any] = {}
        if isinstance(input_data, dict):
            lang = str(input_data.get("language") or "").strip()
            endpoint = str(input_data.get("hint_endpoint") or "").strip()
            region = str(input_data.get("hint_region") or "").strip()
        else:
            lang = endpoint = region = ""
        if not lang:
            lang = (getattr(settings.dify, "chatflow_input_language", "") or "").strip()
        if not endpoint:
            endpoint = (
                getattr(settings.dify, "chatflow_input_hint_endpoint", "") or ""
            ).strip()
        if not region:
            region = (
                getattr(settings.dify, "chatflow_input_hint_region", "") or ""
            ).strip()
        if lang:
            inputs["input_language"] = lang
        if endpoint:
            inputs["input_hint_endpoint"] = endpoint
        if region:
            inputs["input_hint_region"] = region

        # 2) 调 chatflow (透传 conversation_id 续接多轮)
        try:
            logger.info("[DIFY] chatflow end_user=%s files=%d conv=%s", client.end_user, len(files or []), conversation_id or "")
            raw = await client.run_chatflow(
                query=query,
                inputs=inputs,
                files=files or None,
                response_mode="blocking",
                conversation_id=conversation_id or "",
            )
        except DifyError as e:
            logger.error("Dify chatflow error: %s", e)
            raise AIBackendError(f"Dify chatflow 失败: {e}") from e

        # bug 截图跨轮缓存: 本轮若上传了图片, 按 Dify 返回的 conversation_id 记录
        # upload_file_id(目标 app 绑定), 供 /add 写飞书附件时按 conv_id 回取
        # (turn1 发图 turn2 写表)。隔离 try/except, 永不影响主路径。
        try:
            _conv = (raw or {}).get("conversation_id") or ""
            if _conv and upload_file_id:
                _conv_image_put(_conv, upload_file_id)
        except Exception:
            pass

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
            "conversation_id": raw.get("conversation_id") or "",
            "raw": normalized_raw,
        }

    async def run_workflow(
        self,
        input_data: Any,
        user_id: str = "wechat_user",
        conversation_id: Optional[str] = None,
        app: str = "A",
    ) -> Dict[str, Any]:
        """触发 Dify workflow。

        Args:
            input_data: 简化输入,支持两种形态:
                1) ``{"text": str, "file_image_bytes": bytes, "file_image_name": str}``
                   (图片携带原始字节, app 无关; 上传在 _run_chatflow 发送时按目标 app 进行)
                2) 已是 Dify workflow 的 parameters 字典
            user_id: 调用方传入的用户标识(WeChat 场景为 external_userid),
                     会被用作 Dify 的 ``end_user`` 字段。
            app: 双 app 模式下选 "A"(KB问答) 或 "B"(bug追踪); 单 app 模式忽略。

        Returns:
            形如 ``{"content": <reply_text>, "raw": <raw_dify_body>}`` 的字典。
            ``content`` 字段供 ``WeChatService`` 既有的解析逻辑直接读取;
            ``raw`` 字段保留完整响应,便于调试。
        """
        end_user = user_id or settings.dify.end_user_default

        # 若 DifyClient 缓存了默认 end_user 与本次不同,临时构造新 client
        client = self._client_for_app(app)
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
            return await self._run_chatflow(input_data, client, conversation_id)

        # 构造 workflow inputs
        inputs: Dict[str, Any] = {}
        if isinstance(input_data, dict):
            if any(
                k in input_data
                for k in (
                    "text",
                    "file_image_bytes",
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

                # 图片: 携带 app 无关字节, 在发送点按目标 client 上传
                # (Dify 文件库按 app 隔离, 同 _run_chatflow)
                img_bytes = input_data.get("file_image_bytes")
                if img_bytes:
                    img_name = input_data.get("file_image_name") or "image.png"
                    ctype = _detect_image_mime(img_bytes, img_name)
                    _fid = str(await client.upload_file(
                        filename=img_name, content=img_bytes, content_type=ctype,
                    ))
                    _cache_put(_fid, img_bytes, img_name, ctype)
                    inputs[settings.dify.input_image] = [client.file_ref(_fid, "image")]
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

        # 归一化成统一形态,让 MessageProcessor 既有的解析逻辑 (content / data 字段)
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
            # workflow 模式一般无 conversation_id (非 chatflow), 留空保持接口一致
            "conversation_id": (raw or {}).get("conversation_id") or "",
            "raw": raw,
        }

    async def close(self) -> None:
        try:
            await self._http.aclose()
        except Exception as e:
            logger.warning(f"DifyService http client close 失败: {e}")
