"""异常处理模块"""
from typing import Optional, Dict, Any
from fastapi import HTTPException


class WeChatAPIError(Exception):
    """微信API异常"""

    def __init__(self, message: str, code: Optional[int] = None, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.code = code
        self.details = details or {}
        super().__init__(self.message)


class AIBackendError(Exception):
    """AI 后端 (Coze / Dify / ...) 通用异常。"""

    def __init__(self, message: str, code: Optional[int] = None, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.code = code
        self.details = details or {}
        super().__init__(self.message)


# 向后兼容:旧名称仍然可用,新代码请用 AIBackendError
CozeAPIError = AIBackendError


class SessionError(Exception):
    """会话管理异常"""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class ValidationError(HTTPException):
    """数据验证异常"""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        self.details = details or {}
        super().__init__(status_code=422, detail={"message": message, **self.details})


class BusinessError(HTTPException):
    """业务逻辑异常"""

    def __init__(self, message: str, status_code: int = 400, details: Optional[Dict[str, Any]] = None):
        self.details = details or {}
        super().__init__(status_code=status_code, detail={"message": message, **self.details})


def handle_wechat_error(error: WeChatAPIError) -> HTTPException:
    """处理微信API异常"""
    return HTTPException(
        status_code=502,
        detail={
            "error": "wechat_api_error",
            "message": error.message,
            "code": error.code,
            "details": error.details
        }
    )


def handle_coze_error(error: CozeAPIError) -> HTTPException:
    """处理Coze API异常"""
    return HTTPException(
        status_code=502,
        detail={
            "error": "coze_api_error",
            "message": error.message,
            "code": error.code,
            "details": error.details
        }
    )


def handle_session_error(error: SessionError) -> HTTPException:
    """处理会话异常"""
    return HTTPException(
        status_code=500,
        detail={
            "error": "session_error",
            "message": error.message,
            "details": error.details
        }
    )