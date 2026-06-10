"""Dify API 客户端 (dataclass-based, immutable)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import httpx


class DifyError(RuntimeError):
    """Raised for any Dify API failure (HTTP error or workflow-level failure)."""


@dataclass(frozen=True)
class DifyClient:
    api_base: str  # e.g. https://api.dify.ai/v1
    api_key: str   # app-xxx
    end_user: str  # Dify requires a user identifier on every call
    upload_timeout: float = 60.0
    workflow_timeout: float = 120.0

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
        files = {"file": (filename, content, content_type or "application/octet-stream")}
        data = {"user": self.end_user}

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.upload_timeout)) as client:
            resp = await client.post(url, headers=self._headers(), files=files, data=data)

        if resp.status_code >= 400:
            raise DifyError(f"Dify upload failed: HTTP {resp.status_code} {resp.text}")

        body = resp.json()
        file_id = body.get("id")
        if not file_id:
            raise DifyError(f"Dify upload returned no id: {body}")
        return str(file_id)

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

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.workflow_timeout)) as client:
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
            raise DifyError(f"Dify workflow {status}: {err}; outputs={data.get('outputs')}")
        return body

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
