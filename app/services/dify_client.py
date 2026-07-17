"""Dify API 客户端 (dataclass-based, immutable)。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, List, Optional

import httpx


class DifyError(RuntimeError):
    """Raised for any Dify API failure (HTTP error or workflow-level failure)."""


@dataclass(frozen=True)
class DifyClient:
    api_base: str  # e.g. https://api.dify.ai/v1
    api_key: str  # app-xxx
    end_user: str  # Dify requires a user identifier on every call
    upload_timeout: float = 60.0
    workflow_timeout: float = 120.0
    chatflow_timeout: float = 120.0
    app_mode: str = "chatflow"  # "workflow" | "chatflow"

    def _headers(self, *, content_type: Optional[str] = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.api_key}"}
        if content_type:
            h["Content-Type"] = content_type
        return h

    # ------------------------------------------------------------------
    # 1. File upload
    # ------------------------------------------------------------------
    async def upload_file(
        self,
        *,
        filename: str,
        content: bytes,
        content_type: str | None,
    ) -> str:
        """
        Upload a file (image / audio / etc.) to Dify.

        Endpoint:  POST {api_base}/files/upload
        Form:      file (binary), user (string)
        Response:  201 { id, name, mime_type, ... }

        Returns the file's ``id`` (UUID) — used as ``upload_file_id`` later
        when referencing the file in a workflow ``inputs`` file array.
        """
        url = f"{self.api_base.rstrip('/')}/files/upload"
        files = {
            "file": (filename, content, content_type or "application/octet-stream")
        }
        data = {"user": self.end_user}

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.upload_timeout)
        ) as client:
            resp = await client.post(
                url, headers=self._headers(), files=files, data=data
            )

        if resp.status_code >= 400:
            raise DifyError(f"Dify upload failed: HTTP {resp.status_code} {resp.text}")

        body = resp.json()
        file_id = body.get("id")
        if not file_id:
            raise DifyError(f"Dify upload returned no id: {body}")
        return str(file_id)

    async def download_file(self, *, file_id: str) -> bytes:
        """下载已上传文件的内容 (按 file_id 取字节)。

        端点: ``GET {api_base}/files/{file_id}/preview`` (Dify 1.14.2 是
        ``/preview`` 不是 ``/content``; ``/content`` 返回 404)。
        鉴权: 应用 API key (Bearer) + ``user=<end_user>`` query 参数 (Dify
        ``validate_app_token(fetch_from=QUERY)`` 要求)。文件须属于本 app 内某条
        消息 (生产中 turn1 chat-messages 已发送该图, 满足; 独立上传未发送的文件
        会被 _validate_file_ownership 拒绝)。

        用途: bug 截图入飞书附件时, 后端按 Dify upload_file_id 回取图片字节
        (上传时进程内缓存 miss 的兜底路径)。

        Returns:
            文件二进制内容

        Raises:
            DifyError: HTTP 4xx/5xx (如 file_id 不属于本 app -> 404, 用于
                       多 app 场景下逐个 key 尝试)
        """
        url = f"{self.api_base.rstrip('/')}/files/{file_id}/preview"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.upload_timeout)
        ) as client:
            resp = await client.get(
                url, headers=self._headers(), params={"user": self.end_user}
            )

        if resp.status_code >= 400:
            raise DifyError(
                f"Dify download failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        return resp.content

    # ------------------------------------------------------------------
    # 2. Workflow execution
    # ------------------------------------------------------------------
    async def run_workflow(
        self,
        *,
        inputs: dict[str, Any],
        response_mode: str = "blocking",
    ) -> dict[str, Any]:
        """
        Run a Workflow app.

        Endpoint:  POST {api_base}/workflows/run
        Body:      { inputs, response_mode, user }
        Response:  blocking → JSON { task_id, workflow_run_id, data: { status, outputs, error, ... } }

        IMPORTANT: HTTP status is 200 even when ``data.status == "failed"`` —
        the caller MUST inspect ``data.status`` (or catch DifyError explicitly).
        """
        url = f"{self.api_base.rstrip('/')}/workflows/run"
        payload = {
            "inputs": inputs,
            "response_mode": response_mode,
            "user": self.end_user,
        }

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.workflow_timeout)
        ) as client:
            resp = await client.post(
                url,
                headers=self._headers(content_type="application/json"),
                json=payload,
            )

        if resp.status_code >= 400:
            raise DifyError(f"Dify workflow HTTP error: {resp.status_code} {resp.text}")

        body = resp.json()
        data = body.get("data") or {}
        status = data.get("status")
        if status in ("failed", "stopped", "partial-succeeded"):
            # partial-succeeded is included as a soft failure: the workflow
            # ran but at least one node errored. Surface it to the caller.
            err = data.get("error") or "(no error detail)"
            raise DifyError(
                f"Dify workflow {status}: {err}; outputs={data.get('outputs')}"
            )
        return body

    # ------------------------------------------------------------------
    # 3. Chatflow execution (advanced-chat / Chatflow app)
    # ------------------------------------------------------------------
    async def run_chatflow(
        self,
        *,
        query: str,
        inputs: Optional[dict[str, Any]] = None,
        files: Optional[List[dict]] = None,
        response_mode: str = "blocking",
        conversation_id: str = "",
    ) -> dict[str, Any]:
        """
        Run a Chatflow (advanced-chat) app via the ``/v1/chat-messages`` endpoint.

        与 workflow API 不同:
            - 用户文本走 ``query`` 字段 (不是 ``inputs``)
            - 文件走顶层 ``files`` 数组 (不是 ``inputs[<file_var>]``)
            - 响应扁平: ``{event, answer, metadata.retriever_resources, ...}``
              (没有 ``data.outputs`` 嵌套, ``answer`` 直接在顶层)

        Args:
            query:       用户输入/提问内容 (必填)
            inputs:      App 定义的各变量值 (默认 ``{}``)
            files:       文件列表, 每项形如
                ``{"type": "image", "transfer_method": "remote_url", "url": "..."}``
                或 ``{"type": "image", "transfer_method": "local_file", "upload_file_id": "..."}``
            response_mode: ``blocking`` 或 ``streaming``
            conversation_id: 传之前消息的 conversation_id 可续接上下文 (留空 = 新会话)

        Returns:
            blocking 模式: 完整 JSON, 含 ``answer``, ``metadata.retriever_resources`` 等
            streaming 模式: 原始 SSE event 流 (此处仅 blocking 测试覆盖)

        Raises:
            DifyError: HTTP 4xx/5xx (Dify 已认证过 key, 仅在 app 类型不匹配
                       或参数错误时返回 400)
        """
        url = f"{self.api_base.rstrip('/')}/chat-messages"
        payload: dict[str, Any] = {
            "query": query,
            "inputs": inputs or {},
            "response_mode": response_mode,
            "user": self.end_user,
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if files:
            payload["files"] = files

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.chatflow_timeout)
        ) as client:
            resp = await client.post(
                url,
                headers=self._headers(content_type="application/json"),
                json=payload,
            )

        if resp.status_code >= 400:
            raise DifyError(f"Dify chatflow HTTP error: {resp.status_code} {resp.text}")

        return resp.json()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def file_ref(upload_file_id: str, file_type: str) -> dict[str, Any]:
        """
        Build a Dify file-object suitable for a workflow file-array input.

        file_type: 'image' | 'audio' | 'document' | 'video'
        """
        return {
            "type": file_type,
            "transfer_method": "local_file",
            "upload_file_id": upload_file_id,
        }

    def dump_for_debug(self, body: dict[str, Any]) -> str:
        try:
            return json.dumps(body, ensure_ascii=False, indent=2)[:2000]
        except Exception:
            return str(body)[:2000]
