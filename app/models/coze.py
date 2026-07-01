"""Coze相关数据模型"""
from typing import Dict, Any
from enum import Enum
from pydantic import BaseModel, Field


class ActionType(str, Enum):
    """动作类型枚举"""
    REPLY = "reply"
    TRANSFER_HUMAN = "transfer_human"
    QUICK_REPLY = "quick_reply"


class StandardizedMessage(BaseModel):
    """标准化消息格式"""
    user_id: str = Field(..., description="用户ID")
    session_id: str = Field(..., description="会话ID")
    message_type: str = Field(..., description="消息类型")
    content: Dict[str, Any] = Field(default_factory=dict, description="消息内容")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="元数据")
    context: Dict[str, Any] = Field(default_factory=dict, description="上下文")


class CozeWorkflowInput(BaseModel):
    """Coze工作流输入"""
    input_data: StandardizedMessage = Field(..., description="输入数据")


class CozeWorkflowOutput(BaseModel):
    """Coze工作流输出"""
    action: ActionType = Field(..., description="执行动作")
    reply_content: Dict[str, Any] = Field(default_factory=dict, description="回复内容")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="元数据")

    class Config:
        use_enum_values = True

