"""微信服务模块"""
import time
from typing import Optional, List, Dict, Any, TYPE_CHECKING
import httpx
import logging
from datetime import datetime, timedelta
import asyncio

if TYPE_CHECKING:
    from app.services.coze import CozeService

# 微信客服官方SDK
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.exceptions import InvalidSignatureException
from wechatpy.enterprise import WeChatClient
from wechatpy.session.memorystorage import MemoryStorage

import os
from app.core.config import settings


from flask import request
import xml.etree.ElementTree as ET
import base64
from Crypto.Cipher import AES
import struct
import hashlib


from app.models.wechat import (
    WeChatMessage,
    WeChatSyncRequest,
    WeChatSyncResponse,
    WeChatSendMessage,
    WeChatTokenResponse,
    MessageType,
)
from app.core.exceptions import WeChatAPIError

logger = logging.getLogger(__name__)


class WeChatConfig:
    """微信配置类"""
    def __init__(self):
        # 基础企业微信配置
        self.corp_id = settings.wechat.corp_id
        self.corp_secret = settings.wechat.corp_secret.get_secret_value()

        # 微信客服回调配置
        self.kf_token = settings.wechat.kf_token.get_secret_value()
        self.kf_encoding_aes_key = settings.wechat.encoding_aes_key.get_secret_value()

        # 验证配置完整性（可选）
        is_valid = self._validate_config()
        if not is_valid:
            logger.warning("配置验证失败，但将继续运行")
            # 不抛出异常，继续运行

    def _validate_config(self):
        """验证配置完整性"""
        required_fields = {
            'corp_id': self.corp_id,
            'corp_secret': self.corp_secret,
            'kf_token': self.kf_token,
            'kf_encoding_aes_key': self.kf_encoding_aes_key
        }

        # 检查配置是否有效 - 放宽检查条件
        logger.debug(f"验证配置值: corp_id='{self.corp_id}', kf_token='{self.kf_token[:10] if self.kf_token else None}...'")
        missing_fields = [k for k, v in required_fields.items() if not v or str(v) in ['your_corp_id_here', 'your_corp_secret_here', 'your_kf_token_here', 'your_encoding_aes_key_here', 'default_corp_id', 'default_secret', 'default_token']]
        if missing_fields:
            logger.warning(f"缺少必要的配置项: {', '.join(missing_fields)}，将使用默认配置")
            # 设置默认值以避免崩溃
            if not self.corp_id or str(self.corp_id) in ['your_corp_id_here']:
                self.corp_id = 'default_corp_id'
            if not self.corp_secret or str(self.corp_secret) in ['your_corp_secret_here']:
                self.corp_secret = 'default_secret'
            if not self.kf_token or str(self.kf_token) in ['your_kf_token_here']:
                self.kf_token = 'default_token'
            if not self.kf_encoding_aes_key or str(self.kf_encoding_aes_key) in ['your_encoding_aes_key_here']:
                self.kf_encoding_aes_key = 'A' * 43  # 默认43位AES密钥
            return False  # 表示配置不完整

        # 验证EncodingAESKey格式
        if len(self.kf_encoding_aes_key) != 43:
            logger.warning(f"EncodingAESKey长度异常: {len(self.kf_encoding_aes_key)}，期望43位")
            return False

        return True  # 配置验证通过

    def create_wechat_client(self):
        """创建官方微信客户端"""
        try:
            return WeChatClient(
                corp_id=self.corp_id,
                secret=self.corp_secret
            )
        except Exception as e:
            logger.warning(f"创建微信客户端失败: {e}，将使用模拟客户端")
            # 返回一个模拟客户端，避免程序崩溃
            return None

    def create_crypto(self):
        """创建官方微信加密工具"""
        try:
            return WeChatCrypto(
                token=self.kf_token,
                encoding_aes_key=self.kf_encoding_aes_key,
                corp_id=self.corp_id
            )
        except Exception as e:
            logger.warning(f"创建微信加密工具失败: {e}，将使用模拟加密工具")
            # 返回一个模拟加密工具，避免程序崩溃
            return None


# 创建微信配置和服务实例
logger.info(f"配置值检查 - corp_id: '{settings.wechat.corp_id}', kf_token已配置: {bool(settings.wechat.kf_token)}")
try:
    config = WeChatConfig()
    logger.info("微信配置初始化成功")
except Exception as e:
    logger.error(f"微信配置初始化失败: {e}")
    logger.debug(f"原始配置值: corp_id='{settings.wechat.corp_id}', kf_token已配置={bool(settings.wechat.kf_token)}")
    # 创建一个基本的配置对象，使用实际的配置值
    config = WeChatConfig.__new__(WeChatConfig)
    config.corp_id = settings.wechat.corp_id or 'default_corp_id'
    config.corp_secret = settings.wechat.corp_secret.get_secret_value() if settings.wechat.corp_secret else 'default_secret'
    config.kf_token = settings.wechat.kf_token.get_secret_value() if settings.wechat.kf_token else 'default_token'
    config.kf_encoding_aes_key = settings.wechat.encoding_aes_key.get_secret_value() if settings.wechat.encoding_aes_key else 'A' * 43
    logger.warning("使用默认配置")

# 创建官方SDK客户端和服务
try:
    wechat_client = config.create_wechat_client()
    if wechat_client:
        logger.info("微信客户端创建成功")
    else:
        logger.warning("微信客户端创建失败，将使用降级模式")
except Exception as e:
    logger.error(f"创建微信客户端异常: {e}")
    wechat_client = None

try:
    crypto = config.create_crypto()
    if crypto:
        logger.info("微信加密工具创建成功")
    else:
        logger.warning("微信加密工具创建失败，将使用降级模式")
except Exception as e:
    logger.error(f"创建微信加密工具异常: {e}")
    crypto = None

# 创建统一的微信服务类
class WeChatService:
    """微信客服官方SDK服务"""

    # 全局消息去重字典，所有实例共享
    _processed_messages: Dict[str, datetime] = {}
    # 正在处理中的消息集合（防止并发处理）
    _processing_messages: set = set()
    # 已发送回复的消息集合（防止重复发送）
    _sent_replies: set = set()
    # 并发锁保护全局字典
    _lock = asyncio.Lock()

    # 同步状态缓存：为每个open_kfid维护last_cursor
    _sync_states: Dict[str, str] = {}
    # 同步状态锁
    _sync_lock = asyncio.Lock()

    # 事件去重：防止重复处理同一个kf_msg_or_event事件
    _processed_events: Dict[str, datetime] = {}
    # 事件去重锁
    _event_lock = asyncio.Lock()

    def __init__(self, client: WeChatClient = None, crypto: WeChatCrypto = None, config: WeChatConfig = None):
        self.client = client or wechat_client
        self.crypto = crypto or (globals().get('crypto') if 'crypto' in globals() else None)
        self.config = config or (globals().get('config') if 'config' in globals() else None)
        # HTTP客户端用于直接API调用
        self.http_client = httpx.AsyncClient(timeout=30.0)

        # 检查组件是否可用
        if not self.client:
            print("[WARNING] WeChat客户端不可用，某些功能将被禁用")
        if not self.crypto:
            print("[WARNING] WeChat加密工具不可用，消息解密功能将被禁用")

    def verify_signature(self, signature: str, timestamp: str, nonce: str, msg_encrypt: str = "") -> bool:
        """验证签名"""
        try:
            
            # 手动实现签名验证
            import hashlib
            token = self.config.kf_token  # 确保获取字符串值

            params = [token, timestamp, nonce, msg_encrypt]
            
            params.sort()
            tmp_str = ''.join(params)
            expected_signature = hashlib.sha1(tmp_str.encode('utf-8')).hexdigest()

            return expected_signature == signature
        except Exception as e:
            logger.error(f"签名验证异常: {e}")
            return False

    def decrypt_message(self, encrypted_msg: str, signature: str = "", timestamp: str = "", nonce: str = "") -> str:
        """解密消息"""
        if not self.crypto:
            logger.warning("加密工具不可用，返回原始消息")
            return encrypted_msg

        try:
            return self.crypto.decrypt_message(encrypted_msg, signature, timestamp, nonce)
        except Exception as e:
            logger.error(f"消息解密失败: {e}")
            return encrypted_msg
    @staticmethod
    def decrypt_message_custom(encrypted_msg: str, encoding_aes_key: str, corp_id: str) -> str:
        try:
            print("=" * 60)
            print("开始解密...")
            print(f"加密消息长度: {len(encrypted_msg)}")
            
            # 1. 准备AES Key
            # 补全=号（43位 -> 44位，能被4整除）
            if len(encoding_aes_key) != 43:
                raise ValueError(f"EncodingAESKey应该是43位，实际是{len(encoding_aes_key)}位")
            
            aes_key = encoding_aes_key + "="
            key = base64.b64decode(aes_key)
           
            
            # IV是key的前16字节
            iv = key[:16]
            
            
            # 2. Base64解码
            encrypted_data = base64.b64decode(encrypted_msg)
          
            
            # 3. AES-256-CBC解密
            cipher = AES.new(key, AES.MODE_CBC, iv)
            decrypted = cipher.decrypt(encrypted_data)
            
            
            # 4. 调试：查看解密后的前100字节
            
            
            # 5. 尝试不同的填充处理方式
            # 方式1：标准PKCS7去除填充
            try:
                pad = decrypted[-1]
                print(f"最后字节（可能为填充）: {pad}")
                
                if 1 <= pad <= 32:
                    # 正常去除填充
                    unpadded = decrypted[:-pad]
                    print(f"方式1: 去除{pad}字节填充")
                    result = unpadded
                else:
                    # 填充字节无效，尝试其他方式
                    print("方式1: 填充字节无效，尝试方式2")
                    result = decrypted
            except Exception as e:
                print(f"方式1失败: {e}")
                result = decrypted
            
            # 6. 尝试解析数据结构
            try:
                # 企业微信格式：16字节随机数 + 4字节消息长度 + 消息内容 + corp_id
                if len(result) >= 20:
                    random_str = result[:16]
                    msg_len_bytes = result[16:20]
                    msg_len = struct.unpack('>I', msg_len_bytes)[0]
                    
                    print(f"随机字符串长度: 16字节")
                    print(f"消息长度字段: {msg_len}字节")
                    
                    if 20 + msg_len <= len(result):
                        msg_content = result[20:20+msg_len]
                        corp_id_from_msg = result[20+msg_len:]
                        
                        print(f"提取的消息长度: {len(msg_content)}字节")
                        print(f"提取的CorpID长度: {len(corp_id_from_msg)}字节")
                        
                        # 转换为字符串
                        try:
                            msg_str = msg_content.decode('utf-8')
                            corp_str = corp_id_from_msg.decode('utf-8')
                            
                            print(f"提取的CorpID: {corp_str}")
                            
                            # 验证corp_id
                            if corp_str == corp_id:
                                print("✓ CorpID验证通过")
                                return msg_str
                            else:
                                print(f"⚠ CorpID不匹配: 期望={corp_id}, 实际={corp_str}")
                                # 仍然返回内容用于调试
                                return msg_str
                        except UnicodeDecodeError:
                            print("UTF-8解码失败，尝试其他编码")
                    else:
                        print(f"数据长度不足: 需要{20+msg_len}字节，实际{len(result)}字节")
            except Exception as e:
                print(f"结构化解析失败: {e}")
            
            # 7. 如果结构化解析失败，尝试直接查找XML
            print("尝试直接查找XML内容...")
            try:
                # 尝试UTF-8解码
                content = result.decode('utf-8', errors='ignore')
            except:
                content = str(result)
            
            # 查找<xml>标签
            xml_start = content.find('<xml>')
            xml_end = content.find('</xml>')
            
            if xml_start != -1 and xml_end != -1:
                xml_content = content[xml_start:xml_end+6]  # +6 是</xml>的长度
                print(f"✓ 成功提取XML片段")
                return xml_content
            else:
                # 返回原始内容用于调试
                print(f"✗ 未找到XML标签，返回原始内容")
                return content[:500]  # 只返回前500字符避免过长
                
        except Exception as e:
            print(f"解密过程异常: {e}")
            import traceback
            traceback.print_exc()
            raise

    async def get_access_token(self) -> str:
        """获取Access Token"""
        if not self.client:
            raise Exception("WeChat客户端不可用，无法获取Access Token")

        try:
            # wechatpy 会自动管理 access token，直接返回当前的 token
            return self.client.access_token
        except Exception as e:
            print(f"[ERROR] 获取Access Token失败: {e}")
            raise


    async def sync_latest_messages(self, sync_token: str, open_kfid: str = None, max_attempts: int = 2, clear_cursor: bool = False) -> List[WeChatMessage]:
        """高效增量同步最新客户消息
        
        策略：
        1. 优先从最新开始拉取（cursor=None）
        2. 使用增量cursor机制获取更多消息
        3. 智能停止：如果已获取到足够新的消息则提前结束
        4. 去重处理：避免重复消息
        
        Args:
            sync_token: 同步token
            open_kfid: 客服ID（可选）
            max_attempts: 最大尝试次数
            clear_cursor: 是否清除之前保存的cursor
            
        Returns:
            最新的客户消息列表（按send_time降序）
        """
        async with self._sync_lock:
            all_customer_messages = []
            seen_msgids = set()  # 用于去重
            state_key = open_kfid or "default"
            current_time = datetime.now().timestamp()
            
            # 清除之前保存的cursor状态
            if clear_cursor and state_key in self._sync_states:
                logger.info(f"清除之前保存的cursor状态: {state_key}")
                del self._sync_states[state_key]
            
            cursor = None
            attempt = 0
            
            # 策略1：从最新开始拉取
            while attempt < max_attempts:
                try:
                    # 第一次尝试：从最新开始（cursor=None）
                    # 后续尝试：使用上次的next_cursor
                    sync_request = WeChatSyncRequest(
                        token=sync_token,
                        cursor=cursor,
                        limit=100,
                        open_kfid=open_kfid
                    )
                    
                    logger.info(f"同步消息（第{attempt+1}次），cursor={'None' if cursor is None else cursor[:20]+'...'}, limit=100")
                    sync_response = await self.sync_messages(sync_request)
                    
                    # 收集客户消息（去重）
                    batch_count = 0
                    recent_count = 0
                    
                    for msg in sync_response.msg_list:
                        if hasattr(msg, 'origin') and msg.origin == 3:  # 客户消息
                            msgid = getattr(msg, 'msgid', None)
                            if msgid and msgid not in seen_msgids:
                                if hasattr(msg, 'msgid') and hasattr(msg, 'msgtype'):
                                    all_customer_messages.append(msg)
                                    seen_msgids.add(msgid)
                                    batch_count += 1
                                    
                                    # 统计近期消息（5分钟内）
                                    send_time = getattr(msg, 'send_time', 0)
                                    if send_time > (current_time - 300):
                                        recent_count += 1
                    
                    logger.info(f"本次同步: {batch_count}条新消息（累计{len(all_customer_messages)}条），其中{recent_count}条为近期消息")
                    
                    # 如果获取到足够新的消息（至少3条近期消息），可以提前结束
                    if recent_count >= 3 and len(all_customer_messages) >= 10:
                        logger.info(f"已获取到足够的新消息（{recent_count}条近期消息），停止同步")
                        break
                    
                    # 更新cursor用于下次同步
                    if sync_response.next_cursor:
                        cursor = sync_response.next_cursor
                        self._sync_states[state_key] = cursor
                    else:
                        # 没有更多消息了
                        logger.info("没有更多消息，停止同步")
                        break
                    
                    # 如果没有更多消息，退出
                    if not sync_response.has_more:
                        logger.info("API返回has_more=False，停止同步")
                        break
                    
                    attempt += 1
                    
                except Exception as e:
                    logger.error(f"同步消息失败 (attempt {attempt+1}): {e}")
                    attempt += 1
                    if attempt >= max_attempts:
                        break
                    # 等待一小段时间后重试
                    await asyncio.sleep(0.5)
            
            # 按send_time降序排序
            if all_customer_messages:
                all_customer_messages.sort(key=lambda x: getattr(x, 'send_time', 0), reverse=True)
                
                # 记录时间范围
                if len(all_customer_messages) > 0:
                    first_msg = all_customer_messages[0]
                    last_msg = all_customer_messages[-1]
                    first_time = getattr(first_msg, 'send_time', None)
                    last_time = getattr(last_msg, 'send_time', None)
                    
                    if first_time and last_time:
                        try:
                            first_dt = datetime.fromtimestamp(first_time)
                            last_dt = datetime.fromtimestamp(last_time)
                            time_diff = current_time - first_time
                            logger.info(f"同步完成: {len(all_customer_messages)}条消息，最新消息时间: {first_dt.strftime('%Y-%m-%d %H:%M:%S')}（距今{int(time_diff)}秒），最旧: {last_dt.strftime('%Y-%m-%d %H:%M:%S')}")
                        except:
                            logger.info(f"同步完成: {len(all_customer_messages)}条消息")
            
            return all_customer_messages

    async def is_event_processed(self, event_token: str) -> bool:
        """检查kf_msg_or_event事件是否已处理"""
        async with self._event_lock:
            now = datetime.now()
            if event_token in self._processed_events:
                processed_time = self._processed_events[event_token]
                # 30秒内不重复处理同一个事件（缩短窗口，允许更快响应）
                if now - processed_time < timedelta(seconds=30):
                    return True

            # 标记为已处理
            self._processed_events[event_token] = now

            # 清理过期事件记录
            expired_events = [k for k, v in self._processed_events.items()
                             if now - v > timedelta(minutes=5)]
            for k in expired_events:
                del self._processed_events[k]

            return False

    async def sync_messages(self, request: WeChatSyncRequest) -> WeChatSyncResponse:
        """同步消息"""
        access_token = await self.get_access_token()

        url = "https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg"
        headers = {"Content-Type": "application/json"}

        params = {
            "access_token": access_token
        }

        data = {
            "token": request.token,
            "cursor": request.cursor,
            "limit": min(request.limit, 1000),  # 最大1000
            "open_kfid": request.open_kfid
        }

        try:
            logger.info(f"[同步API] 请求参数: cursor={request.cursor[:20] if request.cursor else None}..., limit={data['limit']}, open_kfid={request.open_kfid}")
            response = await self.http_client.post(url, headers=headers, params=params, json=data)
            result = response.json()

            # 记录API响应
            msg_count = len(result.get('msg_list', []))
            errcode = result.get('errcode', 0)

            if errcode != 0:
                logger.warning(f"同步API错误: errcode={errcode}, errmsg={result.get('errmsg')}")
            else:
                logger.debug(f"同步API成功: {msg_count}条消息")

            # 检查API响应是否成功
            errcode = result.get("errcode", 0)  # 默认认为成功
            if errcode != 0:
                raise WeChatAPIError(f"同步消息失败: {result}")

            # 解析消息列表
            msg_list = []
            success_count = 0
            fail_count = 0

            for msg_data in result.get("msg_list", []):
                try:
                    msg = WeChatMessage(**msg_data)
                    msg_list.append(msg)
                    success_count += 1
                except Exception as msg_error:
                    fail_count += 1
                    continue

            # 记录解析结果
            if fail_count > 0:
                logger.warning(f"消息解析完成: 成功{success_count}条，失败{fail_count}条")
            else:
                logger.debug(f"消息解析成功: {success_count}条")

            return WeChatSyncResponse(
                msg_list=msg_list,
                next_cursor=result.get("next_cursor"),
                has_more=result.get("has_more", False),
                errcode=result.get("errcode"),
                errmsg=result.get("errmsg")
            )

        except Exception as e:
            raise WeChatAPIError(f"同步消息异常: {str(e)}")

    async def send_message(self, message: WeChatSendMessage) -> Dict[str, Any]:
        """发送消息"""
        access_token = await self.get_access_token()

        url = "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg"
        headers = {"Content-Type": "application/json"}

        params = {"access_token": access_token}
        data = message.dict(exclude_none=True)

        try:
            response = await self.http_client.post(url, headers=headers, params=params, json=data)
            result = response.json()

            if result.get("errcode") != 0:
                raise WeChatAPIError(f"发送消息失败: {result}")

            return result

        except Exception as e:
            raise WeChatAPIError(f"发送消息异常: {str(e)}")

    async def send_message_simple(self, external_userid: str, kf_account: str, text: str) -> Dict[str, Any]:
        """简化版发送文本消息"""
        logger.info(f"send_message_simple收到参数: external_userid={external_userid}, kf_account={kf_account}, text类型={type(text)}, text值={repr(str(text)[:100])}")

        access_token = await self.get_access_token()

        url = "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg"
        headers = {"Content-Type": "application/json"}

        params = {"access_token": access_token}

        # 确保text是字符串
        if not isinstance(text, str):
            logger.error(f"text参数不是字符串类型: {type(text)}")
            text = str(text)

        data = {
            "touser": external_userid,
            "open_kfid": kf_account,
            "msgtype": "text",
            "text": {
                "content": text
            }
        }

        logger.info(f"发送到微信的数据: {data}")

        try:
            response = await self.http_client.post(url, headers=headers, params=params, json=data)
            result = response.json()

            if result.get("errcode") != 0:
                raise WeChatAPIError(f"发送消息失败: {result}")

            return result

        except Exception as e:
            raise WeChatAPIError(f"发送消息异常: {str(e)}")

    async def download_media(self, media_id: str) -> bytes:
        """下载媒体文件"""
        access_token = await self.get_access_token()

        url = "https://qyapi.weixin.qq.com/cgi-bin/media/get"
        params = {
            "access_token": access_token,
            "media_id": media_id
        }

        try:
            response = await self.http_client.get(url, params=params)

            if response.status_code != 200:
                raise WeChatAPIError(f"下载媒体文件失败: HTTP {response.status_code}")

            return response.content

        except Exception as e:
            raise WeChatAPIError(f"下载媒体文件异常: {str(e)}")

    async def get_user_info(self, external_userid: str, open_kfid: str) -> Dict[str, Any]:
        """获取用户信息"""
        access_token = await self.get_access_token()

        url = "https://qyapi.weixin.qq.com/cgi-bin/kf/customer/get"
        headers = {"Content-Type": "application/json"}

        params = {"access_token": access_token}
        data = {
            "external_userid": external_userid,
            "open_kfid": open_kfid
        }

        try:
            response = await self.http_client.post(url, headers=headers, params=params, json=data)
            result = response.json()

            if result.get("errcode") != 0:
                raise WeChatAPIError(f"获取用户信息失败: {result}")

            return result.get("customer", {})

        except Exception as e:
            raise WeChatAPIError(f"获取用户信息异常: {str(e)}")

    async def process_single_message(self, message: WeChatMessage, coze_service: 'CozeService'):
        """处理单条微信消息
        
        Args:
            message: 微信消息对象
            coze_service: Coze服务实例
        """
        from app.services.coze import CozeService

        # 消息去重检查
        msgid = getattr(message, 'msgid', None)
        if not msgid:
            logger.warning("消息缺少msgid，跳过处理")
            return

        # 使用锁保护去重逻辑，避免并发问题
        async with WeChatService._lock:
            # 检查是否正在处理中
            if msgid in WeChatService._processing_messages:
                logger.info(f"消息 {msgid} 正在处理中，跳过重复处理")
                return

            # 检查是否已处理过（5分钟内）
            now = datetime.now()
            if msgid in WeChatService._processed_messages:
                processed_time = WeChatService._processed_messages[msgid]
                if now - processed_time < timedelta(minutes=5):
                    logger.info(f"消息 {msgid} 已处理过，跳过重复处理")
                    return

            # 立即标记为"处理中"，防止并发处理
            WeChatService._processing_messages.add(msgid)
            logger.info(f"消息 {msgid} 标记为处理中")

            # 清理过期记录（只在处理新消息时清理，避免频繁操作）
            expired_keys = [k for k, v in WeChatService._processed_messages.items()
                           if now - v > timedelta(minutes=5)]
            for k in expired_keys:
                del WeChatService._processed_messages[k]
                # 同时清理已发送回复记录
                WeChatService._sent_replies.discard(k)

        try:
            msgtype = getattr(message, 'msgtype', None)
            if not msgtype:
                logger.error("消息类型为空")
                return

            msgtype_value = msgtype.value if hasattr(msgtype, 'value') else str(msgtype)
            logger.info(f"处理消息类型: {msgtype_value}")

            # 构建输入数据
            input_data = {}
            
            if msgtype_value == 'text':
                # 处理文本消息
                text_content = getattr(message, 'text', None)
                if text_content and isinstance(text_content, dict):
                    content = text_content.get('content', '')
                    input_data['text'] = content
                    logger.info(f"文本消息内容: {content}")
                else:
                    logger.warning("文本消息内容为空")
                    return

            elif msgtype_value == 'image':
                # 处理图片消息
                image_data = getattr(message, 'image', None)
                if image_data and isinstance(image_data, dict):
                    media_id = image_data.get('media_id')
                    if media_id:
                        logger.info(f"[图片消息] 开始处理，media_id: {media_id}")
                        try:
                            # 下载图片
                            logger.info(f"[图片消息] 开始下载图片...")
                            image_content = await self.download_media(media_id)
                            logger.info(f"[图片消息] 图片下载成功，大小: {len(image_content)} 字节")
                            
                            # 上传到Coze并获取文件ID
                            file_name = f"wechat_image_{media_id}.jpg"
                            logger.info(f"[图片消息] 开始上传图片到Coze，文件名: {file_name}")
                            image_file_id = await coze_service.upload_file(image_content, file_name)
                            logger.info(f"[图片消息] 图片上传到Coze成功，文件ID: {image_file_id}")
                            
                            input_data['file_image_id'] = image_file_id
                            logger.info(f"[图片消息] 处理完成，input_data: {input_data}")
                        except Exception as e:
                            logger.error(f"[图片消息] 处理失败: {e}")
                            import traceback
                            logger.error(traceback.format_exc())
                            # 图片处理失败，不继续处理，但会在后续标记为已处理以避免无限重试
                            return
                    else:
                        logger.warning("[图片消息] 未找到media_id")
                        return
                else:
                    logger.warning("[图片消息] 数据为空或格式错误")
                    return

            elif msgtype_value == 'voice':
                # 处理语音消息
                voice_data = getattr(message, 'voice', None)
                if voice_data and isinstance(voice_data, dict):
                    media_id = voice_data.get('media_id')
                    if media_id:
                        logger.info(f"开始下载语音，media_id: {media_id}")
                        try:
                            # 下载语音
                            voice_content = await self.download_media(media_id)
                            logger.info(f"语音下载成功，大小: {len(voice_content)} 字节")
                            
                            # 上传到Coze（使用CozeService的process_wechat_message方法处理语音转换）
                            # 先尝试使用MediaService进行格式转换
                            from app.services.media import MediaService
                            media_service = MediaService(self)
                            
                            media_info = await media_service.download_and_process_media(media_id, 'voice')
                            
                            if media_info.get('error'):
                                # 如果处理失败，使用原始文件
                                logger.warning("语音格式转换失败，使用原始文件")
                                file_name = f"wechat_voice_{media_id}.amr"
                                voice_file_id = await coze_service.upload_file(voice_content, file_name)
                            else:
                                # 使用转换后的文件
                                if media_info.get('converted') and media_info.get('wav_path'):
                                    import aiofiles
                                    async with aiofiles.open(media_info['wav_path'], 'rb') as f:
                                        wav_content = await f.read()
                                    file_name = f"wechat_voice_{media_id}.wav"
                                    voice_file_id = await coze_service.upload_file(wav_content, file_name)
                                else:
                                    file_name = f"wechat_voice_{media_id}.amr"
                                    voice_file_id = await coze_service.upload_file(voice_content, file_name)
                            
                            logger.info(f"语音上传到Coze成功，文件ID: {voice_file_id}")
                            input_data['file_voice_id'] = voice_file_id
                            logger.info(f"语音消息处理完成，input_data: {input_data}")
                        except Exception as e:
                            logger.error(f"处理语音消息失败: {e}")
                            import traceback
                            logger.error(traceback.format_exc())
                            return
                    else:
                        logger.warning("语音消息中未找到media_id")
                        return
                else:
                    logger.warning("语音消息数据为空")
                    return
            else:
                logger.warning(f"不支持的消息类型: {msgtype_value}")
                return

            # 如果没有有效内容，不触发工作流
            if not input_data:
                logger.warning("输入数据为空，跳过工作流触发")
                return

            # 消息内容处理成功（图片/语音已下载上传，或文本已提取），标记为已处理
            # 这样可以避免重复处理，即使后续工作流执行失败也不重复处理
            async with WeChatService._lock:
                WeChatService._processed_messages[msgid] = datetime.now()
                # 从"处理中"集合中移除
                WeChatService._processing_messages.discard(msgid)
                logger.info(f"消息 {msgid} 内容处理成功，已标记为已处理")

            # 获取用户ID
            external_userid = getattr(message, 'external_userid', None) or "wechat_user"
            # 触发Coze工作流
            logger.info(f"触发Coze工作流，用户ID: {external_userid}, 输入数据: {input_data}")
            try:
                
                workflow_result = await coze_service.run_workflow(input_data, user_id=external_userid)
                logger.info("工作流执行成功")

                # 检查数据类型（保留必要的类型检查）
                if isinstance(workflow_result, str):
                    logger.warning("工作流返回字符串，尝试解析JSON")
                    try:
                        import json
                        workflow_result = json.loads(workflow_result)
                        logger.info("JSON解析成功")
                    except json.JSONDecodeError as e:
                        logger.error(f"JSON解析失败: {e}")
                        return

                # 提取工作流返回的文本内容并发送回微信客服
                logger.info(f"最终检查workflow_result类型: {type(workflow_result)}, 是否为dict: {isinstance(workflow_result, dict)}")
                if isinstance(workflow_result, dict):
                    logger.info(f"workflow_result包含的键: {list(workflow_result.keys())}")

                    # 处理run API的响应格式
                    if 'content' in workflow_result:
                        # 直接从content字段提取回复内容
                        reply_text = workflow_result['content']
                        logger.info(f"从run API提取到reply_text: '{reply_text}' (长度: {len(reply_text) if reply_text else 0})")
                        logger.info(f"content_type: {workflow_result.get('content_type', 'unknown')}")
                        logger.info(f"node_type: {workflow_result.get('node_type', 'unknown')}")

                    # 兼容其他可能的格式
                    elif 'data' in workflow_result:
                        raw_data = workflow_result['data']
                        logger.info(f"原始data字段: '{raw_data}' (类型: {type(raw_data)})")

                        # 如果data是字符串，尝试解析JSON
                        if isinstance(raw_data, str):
                            try:
                                import json
                                parsed_data = json.loads(raw_data)
                                logger.info(f"解析JSON成功: {parsed_data}")

                                # 从解析结果中提取实际的回复内容
                                if isinstance(parsed_data, dict) and 'data' in parsed_data:
                                    reply_text = parsed_data['data']
                                    logger.info(f"提取到reply_text: '{reply_text}' (长度: {len(reply_text) if reply_text else 0})")
                                else:
                                    logger.warning(f"解析结果中没有data字段: {parsed_data}")
                                    reply_text = str(parsed_data)
                            except json.JSONDecodeError as e:
                                logger.warning(f"JSON解析失败，使用原始字符串: {e}")
                                reply_text = raw_data
                        else:
                            # 如果data不是字符串，直接使用
                            reply_text = str(raw_data)
                            logger.info(f"data不是字符串，直接使用: '{reply_text}'")
                    else:
                        logger.warning(f"workflow_result中没有找到content或data字段: {workflow_result}")
                        reply_text = ""

                    if reply_text and reply_text.strip():
                        # 检查是否已经发送过回复（防止重复发送）
                        async with WeChatService._lock:
                            if msgid in WeChatService._sent_replies:
                                logger.warning(f"消息 {msgid} 的回复已发送过，跳过重复发送")
                                return
                            # 立即标记为已发送，防止并发发送
                            WeChatService._sent_replies.add(msgid)
                            logger.info(f"消息 {msgid} 标记为已发送回复")

                        # 获取客服ID
                        open_kfid = getattr(message, 'open_kfid', None)
                        logger.info(f"获取到open_kfid: {open_kfid}")
                        if open_kfid:
                            try:
                                # 再次确认reply_text的值
                                logger.info(f"准备发送内容类型: {type(reply_text)}, 值: {repr(reply_text[:100])}")
                                # 发送回复消息回微信客服
                                logger.info(f"发送回复消息到微信客服，用户: {external_userid}, 客服: {open_kfid}, 内容: {reply_text[:50]}...")
                                send_result = await self.send_message_simple(external_userid, open_kfid, reply_text)
                                logger.info(f"回复消息发送成功: {msgid}")

                            except Exception as send_error:
                                logger.error(f"发送回复消息失败: {send_error}")
                                import traceback
                                logger.error(traceback.format_exc())
                                # 发送失败时，从已发送集合中移除，允许重试
                                async with WeChatService._lock:
                                    WeChatService._sent_replies.discard(msgid)
                                    logger.info(f"消息 {msgid} 发送失败，已从已发送集合移除，允许重试")
                        else:
                            logger.warning("消息中未找到open_kfid，无法发送回复")
                    else:
                        logger.warning("工作流返回的文本内容为空")

            except Exception as workflow_error:
                logger.error(f"工作流执行失败: {workflow_error}")
                import traceback
                logger.error(traceback.format_exc())

        except Exception as e:
            logger.error(f"处理消息异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # 处理失败时，从"处理中"集合中移除，允许重试
            async with WeChatService._lock:
                WeChatService._processing_messages.discard(msgid)
                logger.info(f"消息 {msgid} 处理失败，已从处理中集合移除，允许重试")
            raise

    async def close(self):
        """关闭HTTP客户端"""
        await self.http_client.aclose()