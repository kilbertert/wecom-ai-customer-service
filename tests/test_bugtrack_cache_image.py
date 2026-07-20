"""H5 Bug 截图跨轮缓存入口回归。"""

from __future__ import annotations

from io import BytesIO
import json
from unittest.mock import Mock

import pytest
from fastapi import UploadFile
from starlette.requests import Request

import app.routes.bugtrack_internal as route
from app.core.config import settings


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"cache-image-payload"


def _request(ip: str = "124.243.178.156") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/bugtrack/cache-image",
            "headers": [],
            "client": (ip, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


@pytest.mark.asyncio
async def test_cache_image_binds_original_bytes_to_conversation(monkeypatch):
    monkeypatch.setattr(settings.bugtrack, "allowed_ips", "124.243.178.156")
    cache_put = Mock()
    conv_put = Mock()
    monkeypatch.setattr(route, "_cache_put", cache_put)
    monkeypatch.setattr(route, "_conv_image_put", conv_put)

    response = await route.cache_bug_image(
        request=_request(),
        conversation_id="conv-b-123",
        image=UploadFile(filename="screen.png", file=BytesIO(PNG_BYTES)),
    )

    assert response.status_code == 200
    assert json.loads(response.body)["success"] is True
    cache_id = cache_put.call_args.args[0]
    assert cache_id.startswith("h5-")
    assert cache_put.call_args.args[1:] == (
        PNG_BYTES,
        "screen.png",
        "image/png",
    )
    conv_put.assert_called_once_with("conv-b-123", cache_id)


@pytest.mark.asyncio
async def test_cache_image_rejects_fake_image_before_caching(monkeypatch):
    monkeypatch.setattr(settings.bugtrack, "allowed_ips", "124.243.178.156")
    cache_put = Mock()
    monkeypatch.setattr(route, "_cache_put", cache_put)

    response = await route.cache_bug_image(
        request=_request(),
        conversation_id="conv-b-123",
        image=UploadFile(filename="fake.jpg", file=BytesIO(b"not-an-image")),
    )

    assert response.status_code == 400
    cache_put.assert_not_called()


@pytest.mark.asyncio
async def test_cached_h5_image_is_attached_on_later_add(monkeypatch):
    conv_id = "conv-h5-attachment-e2e"
    monkeypatch.setattr(settings.bugtrack, "allowed_ips", "124.243.178.156")
    monkeypatch.setattr(settings.bugtrack, "enabled", True)

    cached = await route.cache_bug_image(
        request=_request(),
        conversation_id=conv_id,
        image=UploadFile(filename="screen.png", file=BytesIO(PNG_BYTES)),
    )
    assert cached.status_code == 200

    captured_fields = {}

    def fake_add(fields):
        captured_fields.update(fields)
        return "rec-cache-e2e"

    monkeypatch.setattr(route, "feishu_upload_attachment", lambda *_args: "file-token-1")
    monkeypatch.setattr(route, "feishu_add_record", fake_add)

    response = await route.add_record_endpoint(
        req=route.AddRecordRequest(
            fields={"模块/功能点": "计费模板管理", "操作描述": "模板保存后未生效"},
            conversation_id=conv_id,
        ),
        request=_request(),
    )

    assert response.status_code == 200
    assert json.loads(response.body)["record_id"] == "rec-cache-e2e"
    assert captured_fields["Bug截图"] == [{"file_token": "file-token-1"}]
    assert route._conv_image_get(conv_id) == []
