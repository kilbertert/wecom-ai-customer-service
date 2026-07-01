"""微信服务模块"""
import traceback
from typing import List, Dict, Any
import httpx
import logging
from datetime import datetime, timedelta
import asyncio

# 微信客服官方SDK
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.enterprise import WeChatClient

from app.core.config import settings


from app.models.wechat import (
    WeChatMessage,
    WeChatSyncRequest,
    WeChatSyncResponse,
    WeChatSendMessage,
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
            logger.warning("WeChat客户端不可用，某些功能将被禁用")
        if not self.crypto:
            logger.warning("WeChat加密工具不可用，消息解密功能将被禁用")

    def verify_signature(self, signature: str, timestamp: str, nonce: str, msg_encrypt: str = "") -> bool:
        """验证签名 (委托 ``WeComCrypto``)。"""
        from app.crypto import wecom_crypto
        return wecom_crypto.verify_signature(
            self.config.kf_token, timestamp, nonce, msg_encrypt, signature
        )

    def verify_bot_signature(self, signature: str, timestamp: str, nonce: str, msg_body: str = "") -> bool:
        """验证群机器人回调签名 (与 KF 同算法, 委托 ``WeComCrypto``)。

        微信群机器人签名算法: SHA1( sort([token, timestamp, nonce, msg_body]) )
        消息体明文 JSON,不加密。

        Args:
            signature: query 参数 msg_signature
            timestamp: query 参数 timestamp
            nonce: query 参数 nonce
            msg_body: 原始 POST body(URL verification 时为 echostr)

        Returns:
            签名是否匹配
        """
        from app.crypto import wecom_crypto
        return wecom_crypto.verify_signature(
            self.config.kf_token, timestamp, nonce, msg_body, signature
        )

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
        """AES 解密 (委托 ``app.crypto.wecom_crypto.decrypt_message``)。

        保留静态方法签名以兼容现有调用方 (route 层 ``WeChatService.decrypt_message_custom(...)``)。
        """
        from app.crypto import wecom_crypto
        return wecom_crypto.decrypt_message(encrypted_msg, encoding_aes_key, corp_id)

    @staticmethod
    def encrypt_message_custom(reply_xml: str, encoding_aes_key: str, corp_id: str, timestamp: str, nonce: str, token: str) -> str:
        """加密回复并返回 XML 信封 (委托 ``app.crypto.wecom_crypto.encrypt_message``)。

        保留静态方法签名以兼容现有调用方 (route 层 ``_build_bot_sync_envelope``)。
        """
        from app.crypto import wecom_crypto
        try:
            return wecom_crypto.encrypt_message(
                reply_xml, encoding_aes_key, corp_id, timestamp, nonce, token
            )
        except Exception as e:
            logger.error(f"encrypt_message_custom 失败: {e}")
            logger.error(traceback.format_exc())
            raise

    async def get_access_token(self) -> str:
        """获取Access Token"""
        if not self.client:
            raise Exception("WeChat客户端不可用，无法获取Access Token")

        try:
            # wechatpy 会自动管理 access token，直接返回当前的 token
            return self.client.access_token
        except Exception as e:
            logger.error(f"获取Access Token失败: {e}")
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

    async def close(self):
        """关闭HTTP客户端"""
        await self.http_client.aclose()