"""数据标准化服务"""
from typing import Dict, Any, Optional
import time

from app.models.wechat import WeChatMessage, MessageType
from app.models.coze import StandardizedMessage


class DataStandardizationService:
    """数据标准化服务"""

    def __init__(self, session_service=None):
        self.session_service = session_service

    async def standardize_wechat_message(self, wechat_msg: WeChatMessage) -> StandardizedMessage:
        """将微信消息转换为标准化格式"""
        # 提取消息内容
        content = self._extract_message_content(wechat_msg)

        # 构建标准化消息
        standardized = StandardizedMessage(
            user_id=wechat_msg.external_userid,
            session_id=f"{wechat_msg.external_userid}_{wechat_msg.send_time}",
            message_type=wechat_msg.msgtype.value,
            content=content,
            metadata={
                "timestamp": wechat_msg.send_time,
                "source": "wechat_kf",
                "kfid": wechat_msg.open_kfid,
                "msg_id": wechat_msg.msgid,
                "origin": wechat_msg.origin
            },
            context={}
        )

        # 添加上下文信息
        await self._add_context_info(standardized)

        return standardized

    def _extract_message_content(self, msg: WeChatMessage) -> Dict[str, Any]:
        """提取消息内容"""
        content = {}

        if msg.msgtype == MessageType.TEXT:
            content["text"] = msg.text.get("content", "") if msg.text else ""

        elif msg.msgtype == MessageType.IMAGE:
            content["media_id"] = msg.image.get("media_id") if msg.image else ""
            # 图片URL需要在下载后生成
            content["url"] = ""

        elif msg.msgtype == MessageType.VOICE:
            content["media_id"] = msg.voice.get("media_id") if msg.voice else ""
            content["format"] = msg.voice.get("format", "amr") if msg.voice else "amr"
            content["url"] = ""

        elif msg.msgtype == MessageType.VIDEO:
            content["media_id"] = msg.video.get("media_id") if msg.video else ""
            content["url"] = ""

        elif msg.msgtype == MessageType.FILE:
            content["media_id"] = msg.file.get("media_id") if msg.file else ""
            content["filename"] = msg.file.get("filename", "") if msg.file else ""
            content["url"] = ""

        elif msg.msgtype == MessageType.LOCATION:
            if msg.location:
                content["coordinates"] = {
                    "lat": msg.location.get("latitude", 0),
                    "lng": msg.location.get("longitude", 0)
                }
                content["label"] = msg.location.get("label", "")

        elif msg.msgtype == MessageType.EVENT:
            content["event_type"] = msg.event.get("event") if msg.event else ""
            content["event_data"] = msg.event or {}

        return content

    async def _add_context_info(self, standardized: StandardizedMessage):
        """添加上下文信息"""
        try:
            if self.session_service:
                # 有会话服务时，获取用户会话历史
                session_data = await self.session_service.get_session(standardized.user_id)

                if session_data:
                    # 构建历史消息
                    history = []
                    for msg in session_data.get_recent_messages(10):
                        history.append({
                            "role": msg.role,
                            "content": msg.content,
                            "timestamp": msg.timestamp
                        })

                    standardized.context["history"] = history
                    standardized.context["session_state"] = {
                        "kf_state": session_data.state.kf_state,
                        "continuous_chat": session_data.state.continuous_chat
                    }

                    # 用户信息
                    if session_data.customer_info:
                        standardized.context["customer_info"] = session_data.customer_info
                else:
                    # 新会话
                    standardized.context["history"] = []
                    standardized.context["session_state"] = {
                        "kf_state": "asking",
                        "continuous_chat": True
                    }
                    standardized.context["customer_info"] = {}
            else:
                # 无会话服务时，使用默认上下文（单轮对话）
                standardized.context = {
                    "history": [],
                    "session_state": {
                        "kf_state": "asking",
                        "continuous_chat": False
                    },
                    "customer_info": {}
                }

        except Exception as e:
            # 上下文获取失败，使用默认值
            print(f"获取上下文失败: {e}")
            standardized.context = {
                "history": [],
                "session_state": {
                    "kf_state": "asking",
                    "continuous_chat": False
                },
                "customer_info": {}
            }

    def standardize_reply_for_wechat(self, coze_output: Dict[str, Any], user_id: str, kfid: str) -> Dict[str, Any]:
        """将Coze输出转换为微信消息格式"""
        action = coze_output.get("action", "reply")
        reply_content = coze_output.get("reply_content", {})

        # 构建微信消息
        wechat_msg = {
            "touser": user_id,
            "open_kfid": kfid,
            "msgtype": reply_content.get("msgtype", "text")
        }

        # 添加消息内容
        if wechat_msg["msgtype"] == "text":
            wechat_msg["text"] = reply_content.get("text", {"content": ""})
        elif wechat_msg["msgtype"] == "image":
            wechat_msg["image"] = reply_content.get("image", {"media_id": ""})
        elif wechat_msg["msgtype"] == "msgmenu":
            wechat_msg["msgmenu"] = reply_content.get("msgmenu", {})

        return wechat_msg

    async def update_session_with_message(self, standardized: StandardizedMessage, role: str = "user"):
        """更新会话消息历史（单轮对话模式下不执行）"""
        if not self.session_service:
            return  # 单轮对话模式，不保存历史

        try:
            # 获取消息内容
            content = ""
            if standardized.message_type == "text":
                content = standardized.content.get("text", "")
            elif standardized.message_type in ["image", "voice", "video", "file"]:
                content = f"[{standardized.message_type}] {standardized.content.get('filename', 'file')}"
            elif standardized.message_type == "location":
                content = "[位置信息]"
            elif standardized.message_type == "event":
                content = f"[事件] {standardized.content.get('event_type', '')}"

            if content:
                await self.session_service.add_message(
                    user_id=standardized.user_id,
                    message=content,
                    role=role,
                    message_type=standardized.message_type
                )

        except Exception as e:
            print(f"更新会话失败: {e}")

    async def update_session_with_reply(self, user_id: str, reply_content: Dict[str, Any]):
        """更新会话回复历史（单轮对话模式下不执行）"""
        if not self.session_service:
            return  # 单轮对话模式，不保存历史

        try:
            content = ""
            msgtype = reply_content.get("msgtype", "text")

            if msgtype == "text":
                content = reply_content.get("text", {}).get("content", "")
            elif msgtype == "image":
                content = "[图片回复]"
            elif msgtype == "msgmenu":
                content = reply_content.get("msgmenu", {}).get("head_content", "[菜单]")

            if content:
                await self.session_service.add_message(
                    user_id=user_id,
                    message=content,
                    role="assistant",
                    message_type=msgtype
                )

        except Exception as e:
            print(f"更新回复会话失败: {e}")