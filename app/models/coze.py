"""Coze相关数据模型"""
from typing import Optional, Dict, Any, List
from enum import Enum
from pydantic import BaseModel, Field


class IntentType(str, Enum):
    """意图类型枚举"""
    PRE_SALE = "售前问题"
    AFTER_SALE = "售后问题"
    FUNCTION_CONSULT = "功能咨询"
    COMPLAINT = "投诉"
    CHAT = "闲聊问候"
    OTHER = "其他"


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


class IntentResult(BaseModel):
    """意图识别结果"""
    intent_type: IntentType = Field(..., description="意图类型")
    confidence: float = Field(..., description="置信度")
    keywords: List[str] = Field(default_factory=list, description="关键词")

    class Config:
        use_enum_values = True


class TransferResult(BaseModel):
    """转人工判断结果"""
    need_transfer: bool = Field(..., description="是否需要转人工")
    score: int = Field(..., description="评分")
    reason: str = Field("", description="原因")


class KnowledgeSearchResult(BaseModel):
    """知识库检索结果"""
    documents: List[Dict[str, Any]] = Field(default_factory=list, description="检索到的文档")
    total_count: int = Field(0, description="总数量")
    search_time: float = Field(0.0, description="检索时间")

