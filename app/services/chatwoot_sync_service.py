"""Chatwoot 同步服务 (Phase 1 + Sprint 2)

负责 wecom-ai → Chatwoot 的三条数据流:
  1. notify_incoming: 收到 WeCom 消息后,把整段会话同步给 Chatwoot
  2. check_handoff (Sprint 2): 调 Chatwoot 查 assignee + online 状态
  3. trigger_bot_handoff (Sprint 2): 调 Chatwoot 触发 Conversation#bot_handoff!
"""
import hmac
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class ChatwootSyncService:
    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._client = http_client
        self.base_url = str(settings.chatwoot.base_url).rstrip("/")
        self.secret = settings.chatwoot.hmac_secret.get_secret_value()
        self.timeout = settings.chatwoot.request_timeout
        self.enabled = settings.chatwoot.enabled

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _sign(self, body: str) -> str:
        return hmac.new(
            self.secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def notify_incoming(
        self,
        open_kfid: str,
        external_userid: str,
        message_data: Dict[str, Any],
        contact_name: Optional[str] = None,
        contact_avatar: Optional[str] = None,
    ) -> bool:
        """把收到的 WeCom 消息同步到 Chatwoot 的 /webhooks/wecom/:open_kfid

        Args:
            open_kfid: 企业微信客服账号 ID (用于 Chatwoot URL 路由)
            external_userid: 客户在 WeCom 内的 user_id (作为 ContactInbox.source_id)
            message_data: 已规范化的消息 dict, 必含 msgid/msgtype, text/image/voice/video/file 任一
            contact_name: 客户显示名 (可选, Chatwoot 用 Haikunator 兜底)
            contact_avatar: 客户头像 URL (可选)

        Returns:
            True 同步成功 (2xx) / False 失败
        """
        if not self.enabled:
            logger.debug("Chatwoot 同步未启用,跳过")
            return False

        url = f"{self.base_url}/webhooks/wecom/{open_kfid}"
        payload = {
            "open_kfid": open_kfid,
            "external_userid": external_userid,
            "contact": {
                "name": contact_name or "",
                "avatar": contact_avatar or "",
            },
            "message": message_data,
        }
        body = json.dumps(payload, ensure_ascii=False)
        signature = self._sign(body)

        headers = {
            "Content-Type": "application/json",
            "X-WecomAI-Signature": signature,
        }

        try:
            resp = await self.client.post(url, headers=headers, content=body)
            if resp.status_code >= 200 and resp.status_code < 300:
                logger.info(
                    f"[Chatwoot] 同步成功: open_kfid={open_kfid}, "
                    f"external_userid={external_userid}, msgid={message_data.get('msgid')}, "
                    f"status={resp.status_code}"
                )
                return True
            else:
                logger.error(
                    f"[Chatwoot] 同步失败: status={resp.status_code}, body={resp.text[:200]}"
                )
                return False
        except httpx.TimeoutException:
            logger.error(f"[Chatwoot] 同步超时: {url}")
            return False
        except Exception as e:
            logger.error(f"[Chatwoot] 同步异常: {e}", exc_info=True)
            return False

    async def check_handoff(
        self,
        open_kfid: str,
        external_userid: str,
    ) -> Dict[str, Any]:
        """Sprint 2: 调 Chatwoot 查 handoff 状态
        有人工 assignee + online 才返回 handoff=True, 跳过 Dify

        GET 请求 body 为空, 签 query string (HMAC-SHA256)
        """
        if not self.enabled:
            return {"handoff": False, "reason": "disabled"}

        # 构造 canonical query string 用于 HMAC 签名
        import urllib.parse
        params = {"open_kfid": open_kfid, "external_userid": external_userid}
        qs = urllib.parse.urlencode(params)
        sig = self._sign(qs)
        url = f"{self.base_url}/public/api/v1/wecom/handoff_status?{qs}"
        headers = {
            "X-WecomAI-Signature": sig,
            "Accept": "application/json",
        }

        try:
            resp = await self.client.get(url, headers=headers)
            if 200 <= resp.status_code < 300:
                logger.info(f"[Chatwoot] handoff check: {resp.json()}")
                return resp.json()
            logger.error(
                f"[Chatwoot] handoff check fail: status={resp.status_code}, body={resp.text[:200]}"
            )
            return {"handoff": False, "reason": f"http_{resp.status_code}"}
        except Exception as e:
            logger.error(f"[Chatwoot] handoff check exception: {e}", exc_info=True)
            return {"handoff": False, "reason": "exception"}

    async def trigger_bot_handoff(
        self,
        open_kfid: str,
        external_userid: str,
        reason: str = "ai_handoff",
    ) -> bool:
        """Sprint 2: 调 Chatwoot 触发 Conversation#bot_handoff!
        触发 CONVERSATION_BOT_HANDOFF dispatcher 事件, agent dashboard 看到

        POST 到 webhooks/wecom/:open_kfid, body 含 reason 字段
        Chatwoot 端 webhooks/wecom_events_job 接 trigger_bot_handoff=true 时调 bot_handoff!
        """
        if not self.enabled:
            return False

        url = f"{self.base_url}/webhooks/wecom/{open_kfid}"
        payload = {
            "open_kfid": open_kfid,
            "external_userid": external_userid,
            "reason": reason,
        }
        body = json.dumps(payload, ensure_ascii=False)
        sig = self._sign(body)
        headers = {
            "Content-Type": "application/json",
            "X-WecomAI-Signature": sig,
        }

        try:
            resp = await self.client.post(url, headers=headers, content=body)
            if 200 <= resp.status_code < 300:
                logger.info(
                    f"[Chatwoot] trigger_bot_handoff: open_kfid={open_kfid}, "
                    f"external_userid={external_userid}, status={resp.status_code}"
                )
                return True
            logger.error(
                f"[Chatwoot] trigger_bot_handoff fail: status={resp.status_code}, body={resp.text[:200]}"
            )
            return False
        except Exception as e:
            logger.error(f"[Chatwoot] trigger_bot_handoff exception: {e}", exc_info=True)
            return False
