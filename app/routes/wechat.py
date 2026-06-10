"""微信回调路由"""
from typing import Optional
from fastapi import APIRouter, Request, Query, HTTPException, BackgroundTasks, Response
from fastapi.responses import PlainTextResponse
import hashlib
import xml.etree.ElementTree as ET
import time
from datetime import datetime
import logging

from app.core.config import settings
from app.services.wechat import WeChatService
from app.services import get_ai_service
from app.models.wechat import WeChatSyncRequest, WeChatMessage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/wechat", tags=["wechat"])


@router.get("/kf/callback")
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
            # 微信客服URL验证签名计算：SHA1(token + timestamp + nonce + echostr)
            token = settings.wechat.kf_token.get_secret_value()
            params = [token, timestamp, nonce, echostr]
            params.sort()
            tmp_str = ''.join(params)
            expected_signature = hashlib.sha1(tmp_str.encode('utf-8')).hexdigest()

            if expected_signature != msg_signature:
                raise Exception("signature verification failed")

            logger.info("[SUCCESS] 签名验证通过")
            logger.info(f"计算的签名: {expected_signature}")
            logger.info(f"收到的签名: {msg_signature}")

            # 步骤2: 解密echostr并返回明文
            # 注意：wechatpy 的 check_signature() 只验证签名，不返回解密内容
            # 所以这里直接用项目里成熟的 decrypt_message_custom 来解密
            logger.info("步骤2: 解密echostr并返回明文...")
            wechat_service = WeChatService()

            decrypted_echostr = wechat_service.decrypt_message_custom(
                echostr,
                wechat_service.config.kf_encoding_aes_key,
                wechat_service.config.corp_id
            )

            logger.info("[SUCCESS] echostr解密成功")
            logger.info(f"解密后内容: {repr(decrypted_echostr)}")
            logger.info("=" * 60)

            # 返回解密后的明文
            return PlainTextResponse(content=decrypted_echostr, media_type="text/plain")

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
                return PlainTextResponse(content=echostr, media_type="text/plain")
            except Exception as fallback_error:
                logger.error(f"[ERROR] 备用方案也失败: {fallback_error}")
                return PlainTextResponse(content="verification failed", media_type="text/plain")

    except Exception as e:
        logger.error(f"[ERROR] 验证过程异常: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        logger.error("=" * 60)
        return PlainTextResponse(content="verification error", media_type="text/plain")


@router.post("/kf/callback")
async def wechat_callback_handler(
    request: Request,
    background_tasks: BackgroundTasks,
    msg_signature: str = Query(..., description="消息签名"),
    timestamp: str = Query(..., description="时间戳"),
    nonce: str = Query(..., description="随机数")
):
    """微信回调消息处理"""
    # 检查User-Agent
    user_agent = request.headers.get('User-Agent')
    # 允许微信相关的User-Agent
    allowed_user_agents = ['WeChat', 'Mozilla/4.0']
    is_allowed = any(agent in (user_agent or '') for agent in allowed_user_agents)

    if not is_allowed:
        logger.warning(f"无效的请求来源，User-Agent: {user_agent}")
        return PlainTextResponse(content="success", media_type="text/plain")

    content_type = request.headers.get('Content-Type')
    received_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        logger.info("=" * 60)
        logger.info(f"[MSG] [{received_time}] 收到微信回调消息")
        logger.info("=" * 60)
        # logger.info(f"User-Agent: {user_agent}")
        # logger.info(f"Content-Type: {content_type}")
        # logger.info(f"消息签名: {msg_signature}")

        if content_type == 'application/json':
            # 处理JSON格式消息
            data = await request.json()
            msg_type = data.get('MsgType')
            logger.info(f"Received {msg_type} message via JSON")

        elif content_type == 'text/xml':
            # 处理XML格式消息
            body = await request.body()
            xml_data = body.decode('utf-8')

            # 步骤0: 预解析XML获取Encrypt内容（用于签名验证）
            msg_encrypt = ""
            try:
                root = ET.fromstring(xml_data)
                encrypt_elem = root.find('Encrypt')
                if encrypt_elem is not None and encrypt_elem.text:
                    msg_encrypt = encrypt_elem.text
                # print(f"[DEBUG] msg_encrypt: {encrypt_elem.text}")
            except Exception as e:
                logger.warning(f"预解析XML失败: {e}")

           
            # 调用解密函数处理加密XML
            try:
                wechat_service = WeChatService()
                # 步骤1: 验证签名
                logger.info("验证签名...")
                is_valid = wechat_service.verify_signature(msg_signature, timestamp, nonce, msg_encrypt)

                if not is_valid:
                    logger.error("签名验证失败")
                    return PlainTextResponse(content="success", media_type="text/plain")

                # 解析XML并检查是否加密
                root = ET.fromstring(xml_data)
                encrypt_elem = root.find('Encrypt')

                if encrypt_elem is not None and encrypt_elem.text:
                    # 解密加密消息
                    try:
                        logger.info(f"[DEBUG] 发现加密消息，Encrypt内容长度: {len(encrypt_elem.text)}")

                        # 使用统一的解密方法
                        decrypted_xml = wechat_service.decrypt_message_custom(
                            encrypt_elem.text,
                            wechat_service.config.kf_encoding_aes_key,
                            wechat_service.config.corp_id
                        )

                        # 解析解密后的XML
                        decrypted_root = ET.fromstring(decrypted_xml)
                        msg_type = decrypted_root.find('MsgType').text if decrypted_root.find('MsgType') is not None else 'unknown'
                    
                        for child in decrypted_root:
                            logger.info(f"  - {child.tag}: {child.text if child.text else '(empty)'}")
                            for subchild in child:
                                logger.info(f"    - {subchild.tag}: {subchild.text if subchild.text else '(empty)'}")

                    except Exception as e:
                        logger.error(f"消息解密失败: {e}")
                        return PlainTextResponse(content="success", media_type="text/plain")

                else:
                    # 未加密消息
                    msg_type = root.find('MsgType').text if root.find('MsgType') is not None else 'unknown'
                    logger.info(f"Received {msg_type} message via plain XML")

            except Exception as e:
                logger.error(f"XML处理失败: {e}")
                return PlainTextResponse(content="success", media_type="text/plain")

        else:
            logger.warning(f"不支持的媒体类型: {content_type}")
            return PlainTextResponse(content="success", media_type="text/plain")

        # 确保decrypted_xml已定义
        if 'decrypted_xml' not in locals():
            logger.error("未找到解密后的XML数据")
            return PlainTextResponse(content="success", media_type="text/plain")

        # 解析解密后的XML
        root = ET.fromstring(decrypted_xml)
        # 检查消息类型
        msg_type_elem = root.find('MsgType')
        if msg_type_elem is None:
            logger.warning("XML中未找到MsgType元素")
            return PlainTextResponse(content="success", media_type="text/plain")

        msg_type = msg_type_elem.text
        logger.info(f"消息类型: {msg_type}")

        # 初始化服务（如果还没有创建）
        if 'wechat_service' not in locals():
            wechat_service = WeChatService()
        coze_service = CozeService()

        # 处理event类型消息（主要是kf_msg_or_event事件）
        if msg_type == 'event':
            event_elem = root.find('Event')
            if event_elem is not None and event_elem.text == 'kf_msg_or_event':
                logger.info("收到客服消息事件(kf_msg_or_event)，开始处理...")

                # 从XML中提取Token和OpenKfid
                token_elem = root.find('Token')
                open_kfid_elem = root.find('OpenKfId')

                if token_elem is None or not token_elem.text:
                    logger.error("XML中未找到Token，无法同步消息")
                    return PlainTextResponse(content="success", media_type="text/plain")

                sync_token = token_elem.text
                open_kfid = open_kfid_elem.text if open_kfid_elem is not None else None

                # 检查事件是否已处理，但即使已处理也尝试同步最新消息
                event_already_processed = await wechat_service.is_event_processed(sync_token)
                if event_already_processed:
                    logger.info(f"事件 {sync_token[:20]}... 已处理过，但仍尝试同步最新消息（可能有新消息）")
                    # 不返回，而是继续同步流程

                logger.info(f"提取到Token: {sync_token[:20]}...")
                if open_kfid:
                    logger.info(f"提取到OpenKfId: {open_kfid}")

                    # 检查是否只处理指定客服的消息
                    allowed_kfid = getattr(settings.wechat, 'allowed_open_kfid', None)
                    if allowed_kfid and open_kfid != allowed_kfid:
                        logger.info(f"跳过非指定客服消息: {open_kfid} (只处理: {allowed_kfid})")
                        return PlainTextResponse(content="success", media_type="text/plain")

                # 使用高效增量同步获取最新客户消息
                # 每次收到新事件时，清除之前保存的cursor，确保从最新消息开始拉取
                try:
                    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    logger.info(f"[{current_time}] 开始高效增量同步消息（清除之前保存的cursor，从最新开始），事件已处理: {event_already_processed}...")
                    all_customer_messages = await wechat_service.sync_latest_messages(
                        sync_token=sync_token,
                        open_kfid=open_kfid,
                        max_attempts=3,  # 增加到3次，确保能获取到最新消息
                        clear_cursor=True  # 清除之前保存的cursor，确保获取最新消息
                    )

                    if all_customer_messages:
                        latest_msg_data = all_customer_messages[0]  # 已经按send_time降序排序，第一个就是最新的
                        current_ts = int(datetime.now().timestamp())
                        latest_ts = int(getattr(latest_msg_data, 'send_time', 0))
                        time_diff = current_ts - latest_ts
                        logger.info(f"同步完成: {len(all_customer_messages)}条消息, 最新消息: msgid={latest_msg_data.msgid}, 发送时间: {datetime.fromtimestamp(latest_ts).strftime('%H:%M:%S')}, 距今: {time_diff}秒")
                    else:
                        logger.info("未找到有效的客户消息")
                        return PlainTextResponse(content="success", media_type="text/plain")

                    msgtype_value = getattr(latest_msg_data, 'msgtype', None)
                    msgtype_str = msgtype_value.value if (msgtype_value and hasattr(msgtype_value, 'value')) else str(msgtype_value) if msgtype_value else 'unknown'

                    logger.info(f"选择处理最新消息: msgid={latest_msg_data.msgid}, 类型={msgtype_str}")

                    # 只记录关键信息，减少详细输出
                    if msgtype_str == 'text' and getattr(latest_msg_data, 'text', None):
                        content = latest_msg_data.text.get('content', '')
                        logger.info(f"文本内容: {content[:50]}{'...' if len(content) > 50 else ''}")
                    elif msgtype_str == 'image' and getattr(latest_msg_data, 'image', None):
                        media_id = latest_msg_data.image.get('media_id', 'unknown')
                        logger.info(f"图片消息: media_id={media_id[:20]}...")
                    elif msgtype_str == 'voice' and getattr(latest_msg_data, 'voice', None):
                        media_id = latest_msg_data.voice.get('media_id', 'unknown')
                        logger.info(f"语音消息: media_id={media_id[:20]}...")

                    # 将消息处理移到后台，避免响应超时
                    logger.info(f"准备添加后台任务处理消息: {latest_msg_data.msgid}")
                    background_tasks.add_task(process_message_background, latest_msg_data)
                    logger.info(f"消息已添加到后台处理队列: {latest_msg_data.msgid}")
                except Exception as sync_error:
                    logger.error(f"同步消息失败: {sync_error}")
                    import traceback
                    logger.error(traceback.format_exc())
            else:
                logger.info(f"收到其他类型事件: {event_elem.text if event_elem else 'unknown'}")
        else:
            logger.info(f"收到非事件类型消息: {msg_type}")

    except Exception as e:
        logger.error(f"处理回调消息异常: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return PlainTextResponse(content="success", media_type="text/plain")

    finally:
        # 清理资源（只清理验证过程中创建的服务实例）
        try:
            if 'wechat_service' in locals() and wechat_service:
                await wechat_service.close()
        except Exception as e:
            logger.warning(f"关闭wechat_service失败: {e}")

    return PlainTextResponse(content="success", media_type="text/plain")


async def process_message_background(message_data):
    """后台处理消息"""
    msgid = getattr(message_data, 'msgid', 'unknown')
    logger.info(f"[后台任务开始] 开始处理消息: {msgid}")

    wechat_service = WeChatService()
    ai_service = get_ai_service()
    try:
        await wechat_service.process_single_message(message_data, ai_service)
        logger.info(f"[后台任务完成] 后台消息处理完成: {msgid}")
    except Exception as e:
        logger.error(f"[后台任务错误] 后台处理消息失败 {msgid}: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        # 清理资源
        try:
            await wechat_service.close()
        except Exception as e:
            logger.warning(f"[后台任务清理] 关闭wechat_service失败: {e}")

        try:
            await ai_service.close()
        except Exception as e:
            logger.warning(f"[后台任务清理] 关闭ai_service失败: {e}")


@router.get("/test")
async def test_endpoint():
    """测试接口"""
    return {"status": "ok", "message": "WeChat callback service is running"}
