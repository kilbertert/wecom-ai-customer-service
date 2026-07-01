"""Chatwoot 调 wecom-ai 的内部端点 (Phase 1)

由 Chatwoot 的 Channel::Wecom 出站 (Wecom::SendOnWecomService) 调用。
"""
import hmac
import hashlib
import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.services.wechat import WeChatService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chatwoot/internal", tags=["chatwoot-internal"])


def _verify_signature(raw_body: bytes, signature: str) -> bool:
    """校验 Chatwoot 调过来的 HMAC-SHA256 签名 (X-WecomAI-Signature)"""
    if not signature:
        return False
    expected = hmac.new(
        settings.chatwoot.hmac_secret.get_secret_value().encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/send_message")
async def send_message_to_wecom(
    request: Request,
    x_wecomai_signature: str = Header(None, alias="X-WecomAI-Signature"),
):
    """Chatwoot agent 在 composer 点发送 → 调这里转发到 WeCom

    Body:
        {
            "open_kfid": "wk_abc",
            "external_userid": "wm_xyz",
            "msgtype": "text",
            "text": {"content": "..."}
        }
    """
    raw_body = await request.body()
    if not _verify_signature(raw_body, x_wecomai_signature or ""):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        body: Dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid json: {e}")

    open_kfid = body.get("open_kfid")
    external_userid = body.get("external_userid")
    msgtype = body.get("msgtype", "text")

    if not open_kfid or not external_userid:
        raise HTTPException(status_code=422, detail="open_kfid and external_userid required")

    wechat_svc = WeChatService()
    try:
        if msgtype == "text":
            text_content = (body.get("text") or {}).get("content", "")
            if not text_content:
                raise HTTPException(status_code=422, detail="text.content required")
            result = await wechat_svc.send_message_simple(external_userid, open_kfid, text_content)
        else:
            raise HTTPException(status_code=501, detail=f"msgtype {msgtype} not yet supported in Phase 1")

        return JSONResponse(content={
            "success": True,
            "msgid": result.get("msgid") if isinstance(result, dict) else None,
        })
    except Exception as e:
        logger.error(f"[chatwoot/internal/send_message] failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)},
        )
    finally:
        await wechat_svc.close()


@router.get("/health")
async def health():
    return {"status": "ok", "chatwoot_enabled": settings.chatwoot.enabled}
