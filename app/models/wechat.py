"""微信相关数据模型"""
from typing import Optional, Dict, Any, List, Union
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field


class MessageType(str, Enum):
    """消息类型枚举"""
    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    FILE = "file"
    LOCATION = "location"
    EVENT = "event"


class EventType(str, Enum):
    """事件类型枚举"""
    USER_ENTER_TEMPSESSION = "user_enter_tempsession"
    MENU_CLICK = "menu_click"
    ASSESSMENT = "assessment"


class WeChatUser(BaseModel):
    """微信用户信息"""
    external_userid: str = Field(..., description="外部用户ID")
    name: str = Field("", description="用户姓名")
    avatar: str = Field("", description="用户头像")
    gender: int = Field(0, description="性别: 0-未知, 1-男性, 2-女性")
    add_time: int = Field(..., description="添加时间")
    type: int = Field(1, description="用户类型")


class WeChatKF(BaseModel):
    """微信客服信息"""
    open_kfid: str = Field(..., description="客服ID")
    name: str = Field("", description="客服名称")


class WeChatMessageContent(BaseModel):
    """微信消息内容"""
    text: Optional[str] = Field(None, description="文本内容")
    media_id: Optional[str] = Field(None, description="媒体文件ID")
    url: Optional[str] = Field(None, description="媒体文件URL")
    coordinates: Optional[Dict[str, float]] = Field(None, description="地理坐标")
    event_type: Optional[EventType] = Field(None, description="事件类型")
    menu_id: Optional[str] = Field(None, description="菜单ID")
    assessment: Optional[int] = Field(None, description="评价分数")


class WeChatMessage(BaseModel):
    """微信消息"""
    msgid: str = Field(..., description="消息ID")
    msgtype: MessageType = Field(..., description="消息类型")
    send_time: int = Field(..., description="发送时间")
    origin: int = Field(..., description="消息来源: 1-用户发送, 2-客服发送")
    external_userid: Optional[str] = Field(None, description="外部用户ID")
    open_kfid: Optional[str] = Field(None, description="客服ID")
    servicer_userid: Optional[str] = Field(None, description="客服用户ID")

    # 消息内容
    text: Optional[Dict[str, Any]] = Field(None, description="文本消息内容")
    image: Optional[Dict[str, Any]] = Field(None, description="图片消息内容")
    voice: Optional[Dict[str, Any]] = Field(None, description="语音消息内容")
    video: Optional[Dict[str, Any]] = Field(None, description="视频消息内容")
    file: Optional[Dict[str, Any]] = Field(None, description="文件消息内容")
    location: Optional[Dict[str, Any]] = Field(None, description="位置消息内容")
    event: Optional[Dict[str, Any]] = Field(None, description="事件消息内容")


class WeChatCallback(BaseModel):
    """微信回调数据"""
    signature: str = Field(..., description="签名")
    timestamp: str = Field(..., description="时间戳")
    nonce: str = Field(..., description="随机数")
    echostr: Optional[str] = Field(None, description="验证字符串")
    xml_data: Optional[str] = Field(None, description="XML数据")


class WeChatSyncRequest(BaseModel):
    """消息同步请求"""
    token: str = Field(..., description="同步token")
    cursor: Optional[str] = Field(None, description="游标")
    limit: int = Field(1000, description="拉取数量")
    open_kfid: Optional[str] = Field(None, description="客服ID")


class WeChatSyncResponse(BaseModel):
    """消息同步响应"""
    msg_list: List[WeChatMessage] = Field(default_factory=list, description="消息列表")
    next_cursor: Optional[str] = Field(None, description="下一个游标")
    has_more: bool = Field(False, description="是否还有更多")
    errcode: Optional[int] = Field(None, description="错误码")
    errmsg: Optional[str] = Field(None, description="错误信息")


class WeChatSendMessage(BaseModel):
    """发送消息请求"""
    touser: str = Field(..., description="接收用户ID")
    open_kfid: str = Field(..., description="客服ID")
    msgid: Optional[str] = Field(None, description="消息ID")
    msgtype: str = Field(..., description="消息类型")

    # 消息内容
    text: Optional[Dict[str, str]] = Field(None, description="文本消息")
    image: Optional[Dict[str, str]] = Field(None, description="图片消息")
    voice: Optional[Dict[str, str]] = Field(None, description="语音消息")
    video: Optional[Dict[str, Any]] = Field(None, description="视频消息")
    file: Optional[Dict[str, Any]] = Field(None, description="文件消息")
    location: Optional[Dict[str, Any]] = Field(None, description="位置消息")
    msgmenu: Optional[Dict[str, Any]] = Field(None, description="菜单消息")


class WeChatTokenResponse(BaseModel):
    """Access Token响应"""
    access_token: str = Field(..., description="访问令牌")
    expires_in: int = Field(..., description="过期时间")
    errcode: Optional[int] = Field(None, description="错误码")
    errmsg: Optional[str] = Field(None, description="错误信息")