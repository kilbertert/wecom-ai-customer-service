#!/usr/bin/env python3

import uvicorn
from fastapi import FastAPI, Request, Query, HTTPException, Response
from fastapi.responses import PlainTextResponse
import json
import time
import hashlib
import os
import base64
import re
import logging
import httpx
import time
from datetime import datetime
from typing import Dict, Any, Optional, List

# 微信客服官方SDK
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.exceptions import InvalidSignatureException
from wechatpy.enterprise import WeChatClient
from wechatpy.session.memorystorage import MemoryStorage

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('wechat_callback.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


# 微信配置类 - 使用官方SDK配置
class WeChatConfig:
    def __init__(self):
        # 基础企业微信配置
        self.corp_id = "wwe23eb3735710f8dc"
        self.corp_secret ="vq8j5OtUETE4-aie8UqBla6O9hQ-9JGaWPLTQ91JB5I"

        # 微信客服回调配置
        self.kf_token = "OCmjYUSjJhpsKUDpneDdWhoI"
        self.kf_encoding_aes_key = "AujPZkqIfghgcqNUQKoFlmmORPekMWhRovTAUBnKfIb"

        # 企业微信应用配置（可选，用于发送消息）
        self.agent_id =""

        # 回调URL配
        self.callback_base_url = ""

        # 验证配置完整性
        self._validate_config()

    def _validate_config(self):
        """验证配置完整性"""
        required_fields = {
            'corp_id': self.corp_id,
            'corp_secret': self.corp_secret,
            'kf_token': self.kf_token,
            'kf_encoding_aes_key': self.kf_encoding_aes_key
        }

        missing_fields = [k for k, v in required_fields.items() if not v or v.startswith('your_')]
        if missing_fields:
            raise ValueError(f"缺少必要的配置项: {', '.join(missing_fields)}")

        # 验证EncodingAESKey格式
        if len(self.kf_encoding_aes_key) != 43:
            raise ValueError("EncodingAESKey长度必须为43位")

        logger.info("微信配置验证通过")

    def create_wechat_client(self):
        """创建官方微信客户端"""
        return WeChatClient(
            corp_id=self.corp_id,
            secret=self.corp_secret
        )

    def create_crypto(self):
        """创建官方微信加密工具"""
        return WeChatCrypto(
            token=self.kf_token,
            encoding_aes_key=self.kf_encoding_aes_key,
            corp_id=self.corp_id
        )
        self._validate_config()

    def _validate_config(self):
        """验证配置完整性"""
        required_configs = [
            ("corp_id", self.corp_id),
            ("corp_secret", self.corp_secret),
            ("kf_token", self.kf_token),
            ("kf_encoding_aes_key", self.kf_encoding_aes_key)
        ]

        missing_configs = [name for name, value in required_configs if not value]
        if missing_configs:
            raise ValueError(f"缺少必要的微信配置: {', '.join(missing_configs)}")

        # 验证EncodingAESKey格式（应为43位Base64编码）
        if len(self.kf_encoding_aes_key) != 43:
            logger.warning(f"EncodingAESKey长度异常: {len(self.kf_encoding_aes_key)}，期望43位")

    def get_crypto(self):
        """获取微信加密解密器实例"""
        return WeChatCrypto(
            token=self.kf_token,
            encoding_aes_key=self.kf_encoding_aes_key,
            corp_id=self.corp_id
        )


# 创建微信配置和服务实例
config = WeChatConfig()

# 创建官方SDK客户端和服务
wechat_client = config.create_wechat_client()
crypto = config.create_crypto()

# 创建统一的微信服务类
class WeChatKFService:
    """微信客服官方SDK服务"""

    def __init__(self, client: WeChatClient, crypto: WeChatCrypto, config: WeChatConfig):
        self.client = client
        self.crypto = crypto
        self.config = config

    def verify_signature(self, signature: str, timestamp: str, nonce: str) -> bool:
        """验证签名"""
        try:
            # 手动实现签名验证（因为 wechatpy 的 check_signature 是用于 echostr 验证的）
            import hashlib

            token = self.config.kf_token
            params = [token, timestamp, nonce]
            params.sort()
            tmp_str = ''.join(params)
            expected_signature = hashlib.sha1(tmp_str.encode('utf-8')).hexdigest()

            return expected_signature == signature
        except Exception:
            return False

    def decrypt_message(self, encrypted_msg: str) -> str:
        """解密消息"""
        return self.crypto.decrypt_message(encrypted_msg)

    async def get_access_token(self) -> str:
        """获取Access Token"""
        # wechatpy 会自动管理 access token，直接返回当前的 token
        return self.client.access_token

# 创建服务实例
wechat_service = WeChatKFService(wechat_client, crypto, config)

app = FastAPI(title="微信客服回调测试", description="监听微信回调并输出消息内容")

# 存储接收到的消息
received_messages = []

# 存储拉取到的具体消息内容
sync_messages = []



# HTTP客户端
client = httpx.AsyncClient(timeout=30.0)

# Access Token 缓存管理器
class AccessTokenManager:
    """微信Access Token缓存管理器"""

    def __init__(self):
        self._token: Optional[str] = None
        self._expires_at: float = 0.0  # 过期时间戳
        self._lock = None  # 在异步环境中使用asyncio.Lock

    async def get_token(self) -> Optional[str]:
        """获取有效的Access Token，自动缓存和刷新"""
        import asyncio
        if self._lock is None:
            self._lock = asyncio.Lock()

        async with self._lock:
            current_time = time.time()

            # 检查缓存是否有效（提前5分钟过期，避免边界问题）
            if self._token and current_time < (self._expires_at - 300):
                return self._token

            # 获取新的token
            token_data = await self._fetch_token()
            if token_data:
                self._token = token_data["access_token"]
                self._expires_at = current_time + token_data["expires_in"]
                logger.info(f"成功获取Access Token，过期时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self._expires_at))}")
                return self._token

            return None

    async def _fetch_token(self) -> Optional[Dict[str, Any]]:
        """从微信API获取Access Token"""
        try:
            url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
            params = {
                "corpid": config.corp_id,
                "corpsecret": config.corp_secret.get_secret_value() if hasattr(config.corp_secret, 'get_secret_value') else config.corp_secret
            }

            logger.info("正在获取Access Token...")
            response = await client.get(url, params=params)
            data = response.json()

            if data.get("errcode") == 0:
                logger.info("Access Token获取成功")
                return {
                    "access_token": data["access_token"],
                    "expires_in": data["expires_in"]
                }
            else:
                logger.error(f"获取Access Token失败: {data}")
                return None

        except Exception as e:
            logger.error(f"获取Access Token异常: {e}")
            return None

    def clear_cache(self):
        """清除缓存的token"""
        self._token = None
        self._expires_at = 0.0
        logger.info("Access Token缓存已清除")

# 创建全局Token管理器实例
token_manager = AccessTokenManager()

# 添加请求日志中间件
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """记录所有请求和响应"""
    if "/wechat/kf/callback" in str(request.url) and request.method == "GET":
        print(f"\n[请求日志] {request.method} {request.url.path}")
        print(f"   来源IP: {request.client.host if request.client else 'unknown'}")
        print(f"   User-Agent: {request.headers.get('user-agent', 'unknown')[:80]}")
        if request.query_params:
            print(f"   查询参数: {dict(request.query_params)}")
    
    response = await call_next(request)
    
    # 记录响应信息
    if "/wechat/kf/callback" in str(request.url) and request.method == "GET":
        print(f"[响应日志] 状态码: {response.status_code}")
        print(f"   Content-Type: {response.headers.get('content-type', 'unknown')}")
        # 注意：这里不能读取响应体，因为它是流式的
        # 但我们可以记录响应头信息
    
    return response

class CallbackConfigManager:
    """回调配置管理器"""

    @staticmethod
    def validate_config() -> Dict[str, Any]:
        """验证回调配置是否符合要求"""
        issues = []

        # 验证Token
        if not config.kf_token:
            issues.append("Token不能为空")
        elif len(config.kf_token) > 32:
            issues.append("Token长度不能超过32位")
        elif not config.kf_token.replace('_', '').replace('-', '').isalnum():
            issues.append("Token只能包含英文、数字、下划线、横线")

        # 验证EncodingAESKey
        if not config.kf_encoding_aes_key:
            issues.append("EncodingAESKey不能为空")
        elif len(config.kf_encoding_aes_key) != 43:
            issues.append(f"EncodingAESKey长度必须为43位，当前长度: {len(config.kf_encoding_aes_key)}")
        elif not config.kf_encoding_aes_key.isalnum():
            issues.append("EncodingAESKey只能包含英文和数字")

        # 验证CorpID
        if not config.corp_id:
            issues.append("CorpID不能为空")

        # 验证CorpSecret
        corp_secret = config.corp_secret.get_secret_value() if hasattr(config.corp_secret, 'get_secret_value') else config.corp_secret
        if not corp_secret:
            issues.append("CorpSecret不能为空")

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "config": {
                "token": config.kf_token[:10] + "..." if config.kf_token else None,
                "encoding_aes_key": config.kf_encoding_aes_key[:10] + "..." if config.kf_encoding_aes_key else None,
                "corp_id": config.corp_id[:10] + "..." if config.corp_id else None,
                "corp_secret_configured": bool(corp_secret)
            }
        }

    @staticmethod
    async def test_access_token() -> Dict[str, Any]:
        """测试Access Token获取"""
        try:
            start_time = time.time()
            token = await wechat_service.get_access_token()
            elapsed = time.time() - start_time

            if token:
                return {
                    "success": True,
                    "token_length": len(token),
                    "elapsed_seconds": round(elapsed, 2),
                    "message": "Access Token获取成功"
                }
            else:
                return {
                    "success": False,
                    "message": "Access Token获取失败",
                    "elapsed_seconds": round(elapsed, 2)
                }
        except Exception as e:
            return {
                "success": False,
                "message": f"Access Token获取异常: {str(e)}"
            }

# 创建回调配置管理器实例
callback_manager = CallbackConfigManager()

# crypto = WeChatCrypto(token = config.kf_token,encoding_aes_key=config.kf_encoding_aes_key,corp_id = config.corp_id)

# @app.get("/wechat/kf/callback")
# async def wechat_callback_verify(
#     request: Request,
#     msg_signature: str = Query(..., description="消息签名"),
#     timestamp: str = Query(..., description="时间戳"),
#     nonce: str = Query(..., description="随机数"),
#     echostr: str = Query(..., description="加密的验证字符串")
# ):
#     logger.info("=" * 60)
#     logger.info("收到微信客服回调验证请求")
#     logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
#     logger.info(f"请求IP: {request.client.host if request.client else 'unknown'}")
#     logger.info(f"消息签名: {msg_signature}")
#     logger.info(f"时间戳: {timestamp}")
#     logger.info(f"随机数: {nonce}")
#     logger.info(f"加密验证字符串: {repr(echostr)}")
#     logger.info(f"字符串长度: {len(echostr)}")
#     logger.info(f"使用Token: {config.kf_token[:10]}...")
#     logger.info(f"企业ID: {config.corp_id}")

#     try:
#         # ============================================
#         # 方法1：使用官方SDK的完整验证（推荐）
#         # ============================================
#         # 注意：企业微信/微信客服的参数是 msg_signature（不是signature）
#         # SDK会同时验证签名并解密echostr
#         decrypted_echostr = wechat_service.crypto.check_signature(
#             signature=msg_signature,
#             timestamp=timestamp,
#             nonce=nonce,
#             msg_encrypt=echostr
#         )

    
        
#         logger.info("[SUCCESS] 签名验证成功")
#         logger.info(f"解密后的echostr: {repr(decrypted_echostr)}")
#         logger.info(f"解密后长度: {len(decrypted_echostr)}")
#         logger.info("=" * 60)
        
#         # 重要：返回解密后的明文echostr
#         # PlainTextResponse确保正确格式（无额外引号、换行等）
#         return PlainTextResponse(
#             content=decrypted_echostr,
#             media_type="text/plain; charset=utf-8"
#         )
        
#     except InvalidSignatureException as e:
#         logger.error("[ERROR] 签名验证失败")
#         logger.error(f"错误信息: {str(e)}")
        
#         # 调试信息：手动计算签名对比
#         # 注意：企业微信的签名计算需要包含加密的echostr
#         params = [config.kf_token, timestamp, nonce, echostr]
#         params.sort()
#         tmp_str = ''.join(params)
#         calculated_sig = hashlib.sha1(tmp_str.encode('utf-8')).hexdigest()
        
#         logger.error(f"计算的签名: {calculated_sig}")
#         logger.error(f"收到的签名: {msg_signature}")
#         logger.error("=" * 60)
        
#         # 企业微信要求1秒内响应，即使验证失败也要快速返回
#         raise HTTPException(
#             status_code=403,
#             detail="Invalid signature"
#         )
        
#     except Exception as e:
#         logger.error("[ERROR] 验证过程中发生异常")
#         logger.error(f"异常类型: {type(e).__name__}")
#         logger.error(f"异常信息: {str(e)}")
#         logger.error("=" * 60)
        
#         # 返回400错误
#         raise HTTPException(
#             status_code=400,
#             detail=f"Verification error: {str(e)}"
#         )

async def get_access_token() -> Optional[str]:
    """获取微信Access Token（使用官方SDK）"""
    try:
        return await wechat_service.get_access_token()
    except Exception as e:
        logger.error(f"获取Access Token失败: {e}")
        return None

async def sync_wechat_messages(token: str, open_kfid: str = None) -> Dict[str, Any]:
    """调用微信客服sync_msg接口拉取消息"""
    try:
        access_token = await get_access_token()
        if not access_token:
            return {"error": "获取Access Token失败"}
        
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg"
        params = {"access_token": access_token}
        
        # 构建请求体
        request_data = {
            "token": token,
            "limit": 1000,
            "voice_format": 0
        }
        
        if open_kfid:
            request_data["open_kfid"] = open_kfid
        
        response = await client.post(url, params=params, json=request_data)
        data = response.json()
        
        if data.get("errcode") == 0:
            logger.info(f"成功拉取消息: 共{len(data.get('msg_list', []))}条")
            return data
        else:
            logger.error(f"拉取消息失败: {data}")
            return {"error": f"拉取失败: {data.get('errmsg', '未知错误')}"}
            
    except Exception as e:
        logger.error(f"拉取消息异常: {e}")
        return {"error": f"拉取异常: {str(e)}"}

def parse_text_message(xml_data) -> Optional[Dict[str, Any]]:
    """解析微信文本消息XML
    
    参考Flask示例代码，解析文本消息内容
    
    Args:
        xml_data: 可以是XML字符串或ET.Element对象
    """
    import xml.etree.ElementTree as ET
    
    try:
        # 如果输入是字符串，解析为ElementTree
        if isinstance(xml_data, str):
            root = ET.fromstring(xml_data)
        elif hasattr(xml_data, 'find'):  # 已经是ET.Element对象
            root = xml_data
        else:
            logger.error(f"不支持的输入类型: {type(xml_data)}")
            return None
        
        msg = {}
        
        # 提取基本字段
        msg["MsgType"] = root.find('MsgType').text if root.find('MsgType') is not None else None
        msg["ToUserName"] = root.find('ToUserName').text if root.find('ToUserName') is not None else None
        msg["FromUserName"] = root.find('FromUserName').text if root.find('FromUserName') is not None else None
        msg["CreateTime"] = root.find('CreateTime').text if root.find('CreateTime') is not None else None
        msg["MsgId"] = root.find('MsgId').text if root.find('MsgId') is not None else None
        
        # 提取文本内容
        content_elem = root.find('Content')
        if content_elem is not None:
            msg["content"] = content_elem.text
        else:
            msg["content"] = None
        
        # 兼容字段名
        msg["to_user"] = msg.get("ToUserName")
        msg["from_user"] = msg.get("FromUserName")
        
        return msg
        
    except ET.ParseError as e:
        logger.error(f"解析文本消息XML失败: {e}")
        return None
    except Exception as e:
        logger.error(f"解析消息异常: {e}")
        return None


def match_keywords(content: str) -> str:
    """关键词匹配函数
    
    根据消息内容匹配关键词并返回回复内容
    可以根据实际需求扩展关键词匹配逻辑
    """
    if not content:
        return "您好，有什么可以帮助您的吗？"
    
    content_lower = content.lower().strip()
    
    # 简单的关键词匹配示例
    keyword_responses = {
        "你好": "您好！欢迎咨询，有什么可以帮助您的吗？",
        "hello": "Hello! How can I help you?",
        "帮助": "我可以帮您解答问题，请告诉我您需要什么帮助。",
        "谢谢": "不客气，很高兴能帮助到您！",
        "再见": "再见，祝您生活愉快！",
    }
    
    # 精确匹配
    if content_lower in keyword_responses:
        return keyword_responses[content_lower]
    
    # 包含匹配
    for keyword, response in keyword_responses.items():
        if keyword in content_lower:
            return response
    
    # 默认回复
    return f"收到您的消息：{content}。我会尽快为您处理，请稍候。"


def build_text_response(to_user: str, from_user: str, content: str) -> str:
    """构建文本消息XML响应
    
    根据微信消息格式构建XML响应
    """
    import xml.etree.ElementTree as ET
    
    # 创建XML根元素
    root = ET.Element("xml")
    
    # 添加字段
    ET.SubElement(root, "ToUserName").text = to_user
    ET.SubElement(root, "FromUserName").text = from_user
    ET.SubElement(root, "CreateTime").text = str(int(time.time()))
    ET.SubElement(root, "MsgType").text = "text"
    ET.SubElement(root, "Content").text = content
    
    # 转换为字符串
    xml_str = ET.tostring(root, encoding='unicode')
    
    return xml_str


def parse_wechat_xml(xml_data: str) -> Dict[str, Any]:
    """解析微信客服回调XML消息"""
    import xml.etree.ElementTree as ET
    
    try:
        root = ET.fromstring(xml_data)
        
        # 根据微信客服API文档，回调事件包含以下字段
        msg_info = {}
        
        # 必填字段
        required_fields = ['ToUserName', 'CreateTime', 'MsgType', 'Event', 'Token', 'OpenKfId']
        
        for field in required_fields:
            element = root.find(field)
            if element is not None:
                msg_info[field.lower()] = element.text
            else:
                logger.warning(f"缺少必填字段: {field}")
        
        # 可选字段
        optional_fields = ['ExternalUserID', 'WelcomeCode', 'FailKfId', 'FailType', 'ChangeType', 'Scene', 'SceneParam']
        
        for field in optional_fields:
            element = root.find(field)
            if element is not None:
                msg_info[field.lower()] = element.text
        
        # 验证必填字段
        if not all(field in msg_info for field in ['tousername', 'createtime', 'msgtype', 'event', 'token', 'openkfid']):
            logger.error("回调消息缺少必填字段")
            return {"error": "missing_required_fields", "raw_xml": xml_data}
        
        # 验证事件类型
        if msg_info.get('msgtype') != 'event' or msg_info.get('event') != 'kf_msg_or_event':
            logger.warning(f"非标准客服消息事件: MsgType={msg_info.get('msgtype')}, Event={msg_info.get('event')}")
        
        return msg_info
        
    except ET.ParseError as e:
        logger.error(f"XML解析失败: {e}")
        return {"error": str(e), "raw_xml": xml_data}

@app.get("/wechat/kf/callback")
async def wechat_callback_verify(
    request: Request,
    msg_signature: str = Query(..., description="消息签名"),
    timestamp: str = Query(..., description="时间戳"),
    nonce: str = Query(..., description="随机数"),
    echostr: str = Query(..., description="加密的验证字符串")
):
    """微信回调URL验证"""
    received_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        logger.info("=" * 60)
        logger.info(f"[VERIFY] [{received_time}] 收到微信回调URL验证请求")
        logger.info("=" * 60)
        logger.info(f"请求来源: {request.client.host if request.client else 'unknown'}")
        logger.info(f"消息签名: {msg_signature}")
        logger.info(f"时间戳: {timestamp}")
        logger.info(f"随机数: {nonce}")
        logger.info(f"验证字符串(加密): {echostr}")

        # 步骤1: 手动验证签名（微信客服签名计算包含echostr）
        logger.info("步骤1: 验证签名（SHA1(token + timestamp + nonce + echostr)）...")
        try:
            import hashlib
            # 微信客服URL验证签名计算：SHA1(token + timestamp + nonce + echostr)
            token = config.kf_token
            params = [token, timestamp, nonce, echostr]
            params.sort()
            tmp_str = ''.join(params)
            expected_signature = hashlib.sha1(tmp_str.encode('utf-8')).hexdigest()

            if expected_signature != msg_signature:
                raise InvalidSignatureException("signature verification failed")

            logger.info("[SUCCESS] 签名验证通过")
            logger.info(f"计算的签名: {expected_signature}")
            logger.info(f"收到的签名: {msg_signature}")

            # 步骤2: 处理echostr
            logger.info("步骤2: 处理echostr...")
            # 微信客服URL验证的echostr是Base64编码的随机字符串
            # 不需要解密，直接返回即可（签名验证已通过）
            logger.info("微信客服URL验证：echostr是随机字符串，无需解密")
            logger.info(f"返回echostr: {repr(echostr)}")
            logger.info(f"echostr长度: {len(echostr)}")
            logger.info("=" * 60)

            # 步骤3: 使用官方SDK解密echostr
            logger.info("步骤3: 使用官方SDK解密echostr...")
            # 微信客服URL验证需要解密echostr后返回明文
            try:
                # 使用wechatpy官方SDK的check_signature方法
                # 该方法会验证签名并返回解密后的echostr
                decrypted_echostr = wechat_service.crypto.check_signature(
                    msg_signature,
                    timestamp,
                    nonce,
                    echostr
                )

                logger.info("[SUCCESS] echostr解密成功（官方SDK）")
                logger.info(f"解密后内容: {repr(decrypted_echostr)}")

                # 返回解密后的明文
                return PlainTextResponse(
                    content=decrypted_echostr,
                    media_type="text/plain"
                )

            except Exception as decrypt_error:
                logger.error(f"[ERROR] echostr解密失败: {decrypt_error}")
                import traceback
                logger.error(traceback.format_exc())

                # 如果官方SDK解密失败，尝试备用方法
                try:
                    logger.info("尝试备用解密方法...")
                    # 将echostr作为已解密的XML直接返回
                    # 某些情况下echostr可能已经是明文
                    logger.warning("返回原始echostr作为备用方案")
                    return PlainTextResponse(
                        content=echostr,
                        media_type="text/plain"
                    )
                except Exception as fallback_error:
                    logger.error(f"[ERROR] 备用方案也失败: {fallback_error}")
                    # 最后的fallback
                    return PlainTextResponse(
                        content="verification failed",
                        media_type="text/plain"
                    )

        except InvalidSignatureException as e:
            logger.error(f"[ERROR] 签名验证失败: {e}")
            logger.error("可能原因:")
            logger.error("  - Token配置不匹配")
            logger.error("  - 签名算法错误")
            logger.error("  - 消息被篡改")
            # 显示计算的签名用于调试
            params = [config.kf_token, timestamp, nonce, echostr]
            params.sort()
            tmp_str = ''.join(params)
            calculated_sig = hashlib.sha1(tmp_str.encode('utf-8')).hexdigest()
            logger.error(f"计算的签名: {calculated_sig}")
            logger.error(f"收到的签名: {msg_signature}")
            logger.error("=" * 60)
            return Response(content="signature verification failed", status_code=403)

    except Exception as e:
        logger.error(f"[ERROR] 验证过程异常: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        logger.error("=" * 60)
        return Response(content="verification error", status_code=500)


@app.post("/wechat/kf/callback")
async def wechat_callback_handler(
    request: Request,
    msg_signature: str = Query(..., description="消息签名"),
    timestamp: str = Query(..., description="时间戳"),
    nonce: str = Query(..., description="随机数")
):
    """微信回调消息处理"""
    # 检查User-Agent
    user_agent = request.headers.get('User-Agent')
    if not user_agent or 'WeChat' not in user_agent:
        logger.warning(f"无效的请求来源，User-Agent: {user_agent}")
        # 无效来源时也返回success，避免重复推送（参考Flask: return "success"）
        return PlainTextResponse(content="success", media_type="text/plain")

    content_type = request.headers.get('Content-Type')
    received_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        logger.info("=" * 60)
        logger.info(f"[MSG] [{received_time}] 收到微信回调消息")
        logger.info("=" * 60)
        logger.info(f"User-Agent: {user_agent}")
        logger.info(f"Content-Type: {content_type}")
        logger.info(f"消息签名: {msg_signature}")

        if content_type == 'application/json':
            # 处理JSON格式消息
            data = await request.json()
            msg_type = data.get('MsgType')
            logger.info(f"Received {msg_type} message via JSON")

            # 处理JSON消息内容
            # 这里可以添加具体的消息处理逻辑

        elif content_type == 'text/xml':
            # 处理XML格式消息
            body = await request.body()
            xml_data = body.decode('utf-8')

            # 调用解密函数处理加密XML
            try:
                # 步骤1: 验证签名
                logger.info("验证签名...")
                is_valid = wechat_service.crypto.check_signature(msg_signature, timestamp, nonce)

                if not is_valid:
                    logger.error("签名验证失败")
                    # 签名验证失败时也返回success，避免重复推送（参考Flask: return "success"）
                    return PlainTextResponse(content="success", media_type="text/plain")

                # 解析XML并检查是否加密
                import xml.etree.ElementTree as ET
                root = ET.fromstring(xml_data)
                encrypt_elem = root.find('Encrypt')

                if encrypt_elem is not None and encrypt_elem.text:
                    # 解密加密消息
                    decrypted_xml = wechat_service.decrypt_message(encrypt_elem.text)
                    # 解析解密后的XML
                    decrypted_root = ET.fromstring(decrypted_xml)
                    msg_type = decrypted_root.find('MsgType').text if decrypted_root.find('MsgType') is not None else 'unknown'
                    logger.info(f"Received {msg_type} message via encrypted XML")
                else:
                    # 未加密消息
                    msg_type = root.find('MsgType').text if root.find('MsgType') is not None else 'unknown'
                    logger.info(f"Received {msg_type} message via plain XML")

                # 处理消息内容
                # 这里可以添加具体的消息处理逻辑

            except Exception as e:
                logger.error(f"XML处理失败: {e}")
                # 处理失败时也返回success，避免重复推送（参考Flask: return "success"）
                return PlainTextResponse(content="success", media_type="text/plain")

        else:
            logger.warning(f"不支持的媒体类型: {content_type}")
            # 不支持的媒体类型也返回success，避免重复推送（参考Flask: return "success"）
            return PlainTextResponse(content="success", media_type="text/plain")

        # 返回成功响应
        logger.info("步骤1: 验证签名...")
        is_valid = wechat_service.verify_signature(msg_signature, timestamp, nonce)
        
        if not is_valid:
            logger.error("[ERROR] 签名验证失败，拒绝处理")
            logger.error("可能原因:")
            logger.error("  - Token配置不匹配")
            logger.error("  - 签名算法错误")
            logger.error("  - 消息被篡改")
            # 显示计算的签名用于调试
            params = [config.kf_token, timestamp, nonce]
            params.sort()
            tmp_str = ''.join(params)
            calculated_sig = hashlib.sha1(tmp_str.encode('utf-8')).hexdigest()
            logger.error(f"计算的签名: {calculated_sig}")
            logger.error(f"收到的签名: {msg_signature}")
            logger.error("=" * 60)
            # 签名验证失败时也返回success，避免重复推送（参考Flask: return "success"）
            return PlainTextResponse(content="success", media_type="text/plain")
        
        logger.info("[SUCCESS] 签名验证通过")
        
        # 步骤2: 获取原始请求数据并解析XML
        logger.info("步骤2: 解析XML格式...")
        body = await request.body()
        raw_xml = body.decode('utf-8')
        
        logger.info(f"原始XML数据长度: {len(raw_xml)}")
        logger.debug(f"原始XML数据: {raw_xml[:500]}...")  # 只记录前500字符
        
        # 解析XML结构
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(raw_xml)
            logger.info("[SUCCESS] XML解析成功")
        except ET.ParseError as e:
            logger.error(f"[ERROR] XML解析失败: {e}")
            # XML解析失败时也返回success，避免重复推送（参考Flask: return "success"）
            return PlainTextResponse(content="success", media_type="text/plain")
        
        # 检查是否是加密消息
        encrypt_type = root.find('Encrypt')
        is_encrypted = encrypt_type is not None and encrypt_type.text
        
        # 步骤3: 解密消息（如果消息是加密的）
        if is_encrypted:
            logger.info("步骤3: 解密加密消息...")
            encrypted_content = encrypt_type.text
            try:
                # 使用WeChatCrypto解密消息
                decrypted_xml = wechat_service.decrypt_message(encrypted_content)
                logger.info("[SUCCESS] 消息解密成功")
                logger.debug(f"解密后的XML: {decrypted_xml[:500]}...")
                
                # 重新解析解密后的XML
                root = ET.fromstring(decrypted_xml)
            except Exception as e:
                logger.error(f"[ERROR] 消息解密失败: {e}")
                # 解密失败时也返回success，避免重复推送（参考Flask: return "success"）
                return PlainTextResponse(content="success", media_type="text/plain")
        else:
            logger.info("步骤3: 消息未加密，跳过解密步骤")

        # 步骤4: 解析消息内容并自动回复
        logger.info("步骤4: 解析消息内容并自动回复...")

        # 检查消息类型
        msg_type_elem = root.find('MsgType')
        if msg_type_elem is None:
            logger.error("消息中没有MsgType字段")
            return PlainTextResponse(content="success", media_type="text/plain")

        msg_type = msg_type_elem.text
        logger.info(f"消息类型: {msg_type}")

        # 只处理文本消息
        if msg_type == 'text':
            # 提取消息内容
            content_elem = root.find('Content')
            if content_elem is not None and content_elem.text:
                content = content_elem.text.strip()
                logger.info(f"收到文本消息: {content}")

                # 关键词匹配并生成回复
                reply_text = match_keywords(content)
                logger.info(f"生成自动回复: {reply_text}")

                # 提取发送者和接收者信息
                from_user_elem = root.find('FromUserName')
                to_user_elem = root.find('ToUserName')

                if from_user_elem is not None and to_user_elem is not None:
                    from_user = from_user_elem.text
                    to_user = to_user_elem.text

                    # 构建XML响应
                    response_xml = build_text_response(
                        to_user=from_user,  # 回复给发送者
                        from_user=to_user,  # 来自客服
                        content=reply_text
                    )

                    logger.info(f"构建的XML响应: {response_xml}")
                    logger.info("=" * 60)

                    # 返回XML响应给微信平台
                    return Response(
                        content=response_xml,
                        media_type="application/xml"
                    )
                else:
                    logger.error("消息中缺少FromUserName或ToUserName字段")
            else:
                logger.warning("文本消息内容为空")
        else:
            logger.info(f"跳过非文本消息类型: {msg_type}")

        logger.info("=" * 60)

        # 返回成功响应（参考Flask: return "success"）
        logger.info("[SUCCESS] 消息处理完成，返回success响应")
        return PlainTextResponse(content="success", media_type="text/plain")

    except Exception as e:
        logger.error(f"[ERROR] 处理失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        logger.error("=" * 60)
        # 处理失败时也要返回success给微信平台，避免重复推送
        # 参考Flask: return "success"
        return PlainTextResponse(content="success", media_type="text/plain")

@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "微信客服回调测试",
        "description": "监听微信回调并输出消息内容",
        "version": "1.1.0",
        "config": {
            "token": config.kf_token[:10] + "...",
            "encoding_aes_key": config.kf_encoding_aes_key[:10] + "...",
            "corp_id": config.corp_id[:10] + "..."
        },
        "endpoints": {
            "callback_verify": "/wechat/kf/callback (GET)",
            "callback_handler": "/wechat/kf/callback (POST)",
            "messages": "/messages (GET)",
            "clear_messages": "/messages (DELETE)",
            "config": "/config (GET)",
            "token": "/token (GET)",
            "health": "/health (GET)",
            "validate_config": "/callback/config/validate (GET)",
            "test_config": "/callback/config/test (POST)",
            "refresh_token": "/callback/token/refresh (POST)"
        },
        "received_messages_count": len(received_messages)
    }

@app.get("/messages")
async def get_messages():
    """获取接收到的消息列表"""
    return {
        "total": len(received_messages),
        "latest_10": received_messages[-10:],  # 返回最近10条消息
        "message_types": get_message_statistics()
    }


@app.get("/config")
async def get_config():
    """获取当前配置信息"""
    return {
        "wechat_config": {
            "token": config.kf_token[:10] + "...",
            "encoding_aes_key": config.kf_encoding_aes_key[:10] + "...",
            "corp_id": config.corp_id[:10] + "...",
            "corp_secret": config.corp_secret[:10] + "..."
        },
        "server_info": {
            "host": "0.0.0.0",
            "port": 8000,
            "log_file": "wechat_callback.log"
        }
    }

@app.get("/health")
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "received_messages": len(received_messages)
    }

@app.get("/callback/config/validate")
async def validate_callback_config():
    """验证回调配置"""
    return callback_manager.validate_config()


@app.get("/test/enter")
async def test_enter_method(echostr: str):
    """测试进入方法的简单接口"""
    # 临时方案：直接返回echostr，通过验证
    print(f"直接返回echostr: {echostr}")
    return Response(content=echostr)

@app.get("/wechat/verify_test")
async def verify_test(
    request: Request,
    signature: str = "",
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = ""
):
    """专门用于调试微信验证的端点"""
    # 记录所有可能的参数名
    actual_signature = signature or msg_signature
    
    print("=== 微信验证测试 ===")
    print(f"实际signature参数名: {'signature' if signature else 'msg_signature'}")
    print(f"signature值: {actual_signature}")
    print(f"timestamp: {timestamp}")
    print(f"nonce: {nonce}")
    print(f"echostr: {echostr}")
    
    # 直接通过验证（用于测试）
    return Response(content=echostr or "SUCCESS", media_type="text/plain")

@app.post("/callback/config/test")
async def test_callback_config():
    """测试回调配置是否正确"""
    try:
        logger.info("开始测试回调配置...")

        # 测试配置验证
        validation = callback_manager.validate_config()
        if validation["valid"]:
            logger.info("✓ 配置验证通过")
        else:
            logger.warning("⚠ 配置验证失败")
            return Response(content=f"Config validation failed: {', '.join(validation['issues'])}", status_code=400)

        # 测试WeChat SDK初始化
        try:
            test_client = config.create_wechat_client()
            test_crypto = config.create_crypto()
            logger.info("✓ WeChat SDK初始化成功")
        except Exception as e:
            logger.error(f"✗ WeChat SDK初始化失败: {e}")
            return Response(content=f"WeChat SDK initialization failed: {str(e)}", status_code=500)

        # 测试Access Token获取
        try:
            token = await wechat_service.get_access_token()
            if token:
                logger.info("✓ Access Token获取成功")
            else:
                logger.warning("⚠ Access Token获取失败")
                return Response(content="Access Token获取失败", status_code=500)
        except Exception as e:
            logger.error(f"✗ Access Token获取异常: {e}")
            return Response(content=f"Access Token获取异常: {str(e)}", status_code=500)

        # 测试签名验证
        try:
            test_timestamp = "1234567890"
            test_nonce = "testnonce"
            # 生成测试签名
            import hashlib
            params = [config.kf_token, test_timestamp, test_nonce]
            params.sort()
            tmp_str = ''.join(params)
            test_signature = hashlib.sha1(tmp_str.encode('utf-8')).hexdigest()

            is_valid = wechat_service.verify_signature(test_signature, test_timestamp, test_nonce)
            if is_valid:
                logger.info("✓ 签名验证功能正常")
            else:
                logger.warning("⚠ 签名验证功能异常")
                return Response(content="签名验证功能异常", status_code=500)
        except Exception as e:
            logger.error(f"✗ 签名验证测试失败: {e}")
            return Response(content=f"签名验证测试失败: {str(e)}", status_code=500)

        logger.info("✓ 所有测试通过，回调配置正确")
        return Response(content="success", status_code=200)

    except Exception as e:
        logger.error(f"配置测试异常: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return Response(content=f"配置测试异常: {str(e)}", status_code=500)


@app.post("/callback/token/refresh")
async def refresh_access_token():
    """强制刷新Access Token"""
    try:
        # 清除缓存
        token_manager.clear_cache()

        # 获取新token
        token = await get_access_token()

        if token:
            return {
                "success": True,
                "message": "Access Token刷新成功",
                "token_length": len(token)
            }
        else:
            return {
                "success": False,
                "message": "Access Token刷新失败"
            }
    except Exception as e:
        return {
            "success": False,
            "message": f"Access Token刷新异常: {str(e)}"
        }

def get_message_statistics() -> Dict[str, int]:
    """获取消息统计信息"""
    stats = {
        "total": len(received_messages),
        "event_kf_msg_or_event": 0,
        "event_enter_session": 0,
        "event_msg_send_fail": 0,
        "msgtype_text": 0,
        "msgtype_image": 0,
        "msgtype_voice": 0,
        "parse_failed": 0
    }
    
    for msg in received_messages:
        info = msg["info"]
        
        if "error" in info:
            stats["parse_failed"] += 1
            continue
            
        if info.get("event") == "kf_msg_or_event":
            stats["event_kf_msg_or_event"] += 1
        elif info.get("event") == "enter_session":
            stats["event_enter_session"] += 1
        elif info.get("event") == "msg_send_fail":
            stats["event_msg_send_fail"] += 1
            
        if info.get("msgtype") == "text":
            stats["msgtype_text"] += 1
        elif info.get("msgtype") == "image":
            stats["msgtype_image"] += 1
        elif info.get("msgtype") == "voice":
            stats["msgtype_voice"] += 1
            
    return stats

def print_startup_info():
    logger.info("")
    logger.info("使用说明:")
    logger.info("1. 先运行: POST http://localhost:8000/callback/config/test 验证配置")
    logger.info("2. 确保ngrok正在运行: ngrok http 8000")
    logger.info("3. 获取ngrok URL，例如: https://xxx.ngrok.io")
    logger.info("4. 在微信后台配置回调URL: https://xxx.ngrok.io/wechat/kf/callback")
    logger.info("5. 三个参数(Token、EncodingAESKey、URL)必须与微信后台配置完全一致")
    logger.info("6. 让用户通过微信客服发送消息")
    logger.info("7. 查看控制台输出的消息信息")
    logger.info("8. 访问 http://localhost:8000/messages 查看接收到的消息")
    logger.info("=" * 60)
    logger.info("服务器启动成功，等待微信回调...")

async def validate_config_on_startup():
    """启动时验证回调配置"""
    logger.info("正在验证回调配置...")
    validation = callback_manager.validate_config()

    if validation["valid"]:
        logger.info("✅ 回调配置验证通过")
        logger.info(f"   Token长度: {len(config.kf_token)}位")
        logger.info(f"   EncodingAESKey长度: {len(config.kf_encoding_aes_key)}位")

        # 测试Access Token获取
        logger.info("正在测试Access Token获取...")
        token_test = await callback_manager.test_access_token()
        if token_test["success"]:
            logger.info("✅ Access Token获取测试通过")
        else:
            logger.warning(f"⚠️ Access Token获取测试失败: {token_test['message']}")
    else:
        logger.error("❌ 回调配置验证失败:")
        for issue in validation["issues"]:
            logger.error(f"   - {issue}")
        logger.error("请检查环境变量配置")

if __name__ == "__main__":
    # 运行配置验证
    import asyncio
    asyncio.run(validate_config_on_startup())

    print_startup_info()

    # 启动服务器
    uvicorn.run(
        "test_wechat_callback:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info"
    )