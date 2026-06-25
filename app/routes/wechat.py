"""微信回调路由"""
from typing import Optional, Dict, Set, Any
import asyncio
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
from app.services.multimodal import compose_multimodal_markdown
from app.models.wechat import WeChatSyncRequest, WeChatMessage


# 智能机器人后台任务 dedup: 同一 msgid 在 ttl 秒内不重复触发
_bot_processed_msgs: Dict[str, float] = {}
_bot_dedup_lock = asyncio.Lock()
_BOT_MSG_TTL = 600  # 10 分钟内不重复处理同一 msgid (防止 WeChat 重试风暴)

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
        ai_service = get_ai_service()

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


# ============================================================================
# 微信群机器人回调（区别于客服：明文 JSON 协议，无需 AES 解密）
# ============================================================================

@router.get("/bot/callback")
async def bot_callback_verify(
    msg_signature: str = Query(..., description="消息签名(SHA1)"),
    timestamp: str = Query(..., description="时间戳"),
    nonce: str = Query(..., description="随机数"),
    echostr: str = Query(..., description="回显字符串(加密)"),
):
    """企业微信智能机器人 URL 验证

    与客服不同: 智能机器人的 echostr 是 AES 加密字符串
    服务端必须:
      1) SHA1(sort([token, timestamp, nonce, echostr])) 验签
      2) AES 解密 echostr (receive_id="", 企业自建)
      3) 返回解密后的 msg 明文
    """
    logger.info(f"[BOT VERIFY] timestamp={timestamp} nonce={nonce} echostr_len={len(echostr)}")
    try:
        svc = WeChatService()
        # 1) 验签（用加密的 echostr 原文参与签名计算）
        if not svc.verify_bot_signature(msg_signature, timestamp, nonce, echostr):
            logger.warning("[BOT VERIFY] 签名验证失败")
            return Response(
                status_code=403,
                content="signature verification failed",
                media_type="text/plain",
                headers={"Content-Type": "text/plain"},
            )

        # 2) AES 解密 echostr —— 企业自建智能机器人 receive_id 传空字符串 ""
        aes_key = svc.config.kf_encoding_aes_key
        try:
            plaintext_msg = WeChatService.decrypt_message_custom(
                echostr, aes_key, ""  # receive_id=""
            )
        except Exception as e:
            logger.error(f"[BOT VERIFY] AES 解密 echostr 失败: {e}")
            return Response(
                status_code=500,
                content=f"decrypt failed: {e}",
                media_type="text/plain",
                headers={"Content-Type": "text/plain"},
            )

        logger.info(f"[BOT VERIFY] 验签+解密通过, 返回明文 (长度 {len(plaintext_msg)})")

        # 3) 返回明文 msg —— 不能加引号/BOM/换行
        return Response(
            status_code=200,
            content=plaintext_msg,
            media_type="text/plain",
            headers={"Content-Type": "text/plain"},
        )
    except Exception as e:
        logger.error(f"[BOT VERIFY] 异常: {e}")
        return Response(
            status_code=500,
            content=f"error: {e}",
            media_type="text/plain",
            headers={"Content-Type": "text/plain"},
        )


@router.post("/bot/callback")
async def bot_callback_handler(
    request: Request,
    msg_signature: str = Query(..., description="消息签名(SHA1)"),
    timestamp: str = Query(..., description="时间戳"),
    nonce: str = Query(..., description="随机数"),
):
    """企业微信智能机器人消息回调 (一期重做: 异步化 + dedup)

    协议关键点（与客服不同）:
      - POST body 是 JSON `{"encrypt": "B64_..."}`（不是 XML）
      - 签名: SHA1(sort([token, timestamp, nonce, encrypt_value]))
      - 解密: AES-256-CBC, receive_id="" (企业自建固定空字符串)
      - 解密后是 JSON: {msgid, aibotid, chattype, from.userid, msgtype, text.content, response_url, ...}
      - 异步响应 (response_url): aibot/response 接口只接受 msgtype="markdown"

    一期改造 (2026-06-24):
      - 解密 + dedup 检查后立即返回占位 envelope, Dify workflow 调用移入后台 task
      - 防止智能机器人 5s callback 超时重试 (Dify 跑 30-50s)
      - 防止同一 msgid 被多次处理 (errcode 60140 重复响应)
    """
    import json as _json
    body_str = (await request.body()).decode("utf-8", errors="ignore")
    logger.info(f"[BOT MSG] ts={timestamp} nonce={nonce} body_len={len(body_str)}")

    try:
        # 1) 解析外部 JSON + 验签
        try:
            data = _json.loads(body_str)
        except _json.JSONDecodeError as e:
            return Response(status_code=400, content=f"invalid json: {e}")
        msg_encrypt = (data.get("encrypt") or "").strip()
        if not msg_encrypt:
            return Response(status_code=400, content="missing encrypt")

        svc = WeChatService()
        if not svc.verify_bot_signature(msg_signature, timestamp, nonce, msg_encrypt):
            return Response(status_code=403, content="signature verification failed")
        logger.info("[BOT MSG] 签名验证通过")

        # 2) AES 解密 (receive_id="")
        try:
            decrypted = WeChatService.decrypt_message_custom(
                msg_encrypt, svc.config.kf_encoding_aes_key, ""
            )
        except Exception as e:
            logger.error(f"[BOT MSG] AES 解密失败: {e}")
            return Response(status_code=500, content=f"decrypt failed: {e}")

        # 3) 解析内层 JSON 消息
        try:
            msg = _json.loads(decrypted)
        except _json.JSONDecodeError as e:
            logger.error(f"[BOT MSG] 内层 JSON 解析失败: {e}")
            return Response(status_code=500, content=f"invalid decrypted json: {e}")

        from_user = (msg.get("from") or {}).get("userid") or "unknown"
        msg_type = msg.get("msgtype") or ""
        msg_id = msg.get("msgid") or ""
        text_obj = msg.get("text") or {}
        content = (text_obj.get("content") if isinstance(text_obj, dict) else text_obj) or ""
        content = str(content).strip()
        response_url = msg.get("response_url") or ""

        # 一期增强: 识别 image / voice / mixed 消息, 提取媒体定位符
        # 关键差异:
        #   - 智能机器人 image 消息: msg.image.url  (微信 CDN 直链 URL, 直接 httpx.GET)
        #   - 智能机器人 voice 消息: msg.voice.media_id  (走 /cgi-bin/media/get 下载)
        #   - 智能机器人 mixed 消息 (图文混合): msg.mixed 包含 text + image 等子结构
        #   - 客服 kf 两种都有, 但目前 bot 路径只走智能机器人
        # 后台任务会用 httpx 直接 GET url 拿 bytes, 或调 WeChatService.download_media 拿 bytes
        wechat_media_ref = ""
        wechat_media_kind = ""  # "url" | "media_id"
        # effective_media_type 用于后台任务的 media 上传分支判断
        # - msg_type=image  → "image"
        # - msg_type=voice  → "voice"
        # - msg_type=mixed  → mixed 里 image 子项时 "image", voice 时 "voice"
        # - msg_type=text   → ""
        effective_media_type = ""
        if msg_type == "image":
            image_obj = msg.get("image") or {}
            if isinstance(image_obj, dict):
                url_val = (image_obj.get("url") or "").strip()
                mid_val = (image_obj.get("media_id") or "").strip()
                if url_val:
                    wechat_media_ref = url_val
                    wechat_media_kind = "url"
                elif mid_val:
                    wechat_media_ref = mid_val
                    wechat_media_kind = "media_id"
            effective_media_type = "image" if wechat_media_ref else ""
        elif msg_type == "voice":
            voice_obj = msg.get("voice") or {}
            if isinstance(voice_obj, dict):
                mid_val = (voice_obj.get("media_id") or "").strip()
                if mid_val:
                    wechat_media_ref = mid_val
                    wechat_media_kind = "media_id"
            effective_media_type = "voice" if wechat_media_ref else ""
        elif msg_type == "mixed":
            # 智能机器人图文混合消息实际结构 (DIAG 确认):
            #   {msgtype: "mixed", mixed: {msg_item: [{msgtype, ...}, ...]}}
            # msg_item 是数组, 每个元素像单条消息: {msgtype, text:{content}, image:{url}} 等
            mixed_obj = msg.get("mixed") or {}
            items = []
            if isinstance(mixed_obj, dict):
                items = mixed_obj.get("msg_item") or mixed_obj.get("items") or []
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    itype = item.get("msgtype") or ""
                    if not content and itype == "text":
                        t = item.get("text") or {}
                        if isinstance(t, dict):
                            content = (t.get("content") or "").strip()
                    elif itype == "image" and not wechat_media_ref:
                        img = item.get("image") or {}
                        if isinstance(img, dict):
                            url_val = (img.get("url") or "").strip()
                            mid_val = (img.get("media_id") or "").strip()
                            if url_val:
                                wechat_media_ref = url_val
                                wechat_media_kind = "url"
                                effective_media_type = "image"
                            elif mid_val:
                                wechat_media_ref = mid_val
                                wechat_media_kind = "media_id"
                                effective_media_type = "image"
                    elif itype == "voice" and not wechat_media_ref:
                        v = item.get("voice") or {}
                        if isinstance(v, dict):
                            mid_val = (v.get("media_id") or "").strip()
                            if mid_val:
                                wechat_media_ref = mid_val
                                wechat_media_kind = "media_id"
                                effective_media_type = "voice"

        logger.info(
            f"[BOT MSG] from={from_user} type={msg_type} "
            f"content_len={len(content)} id={msg_id} "
            f"media_ref={wechat_media_kind}:{bool(wechat_media_ref)}"
        )

        # 4) dedup 检查 (一期新增): 同一 msgid 短时间内不重复处理
        now_ts = time.time()
        async with _bot_dedup_lock:
            # 清理过期记录
            expired = [k for k, v in _bot_processed_msgs.items() if now_ts - v > _BOT_MSG_TTL]
            for k in expired:
                _bot_processed_msgs.pop(k, None)
            if msg_id and msg_id in _bot_processed_msgs:
                logger.info(
                    f"[BOT MSG] msgid {msg_id} 已处理过 (dedup), 立即返回占位 envelope"
                )
                placeholder = _build_bot_sync_envelope(svc, "AI 正在处理中...", timestamp, nonce)
                return Response(status_code=200, content=placeholder, media_type="application/json")
            # 标记处理中
            if msg_id:
                _bot_processed_msgs[msg_id] = now_ts

        # 5) 创建后台任务异步处理 (Dify 跑 30-50s, 不能同步等)
        asyncio.create_task(
            _process_bot_message_background(
                msg=msg,
                from_user=from_user,
                msg_type=msg_type,
                effective_media_type=effective_media_type,
                content=content,
                msg_id=msg_id,
                response_url=response_url,
                wechat_media_ref=wechat_media_ref,
                wechat_media_kind=wechat_media_kind,
                timestamp=timestamp,
                nonce=nonce,
                encoding_aes_key=svc.config.kf_encoding_aes_key,
                kf_token=svc.config.kf_token,
            ),
            name=f"bot-{msg_id[:8] if msg_id else 'noid'}",
        )
        logger.info(f"[BOT MSG] msgid={msg_id} 已创建后台任务, 立即返回占位 envelope")

        # 6) 立即返回占位 envelope (避免 WeChat 5s 超时重试)
        placeholder = _build_bot_sync_envelope(svc, "AI 正在处理中...", timestamp, nonce)
        return Response(
            status_code=200,
            content=placeholder,
            media_type="application/json",
        )
    except Exception as e:
        import traceback
        logger.error(f"[BOT MSG] 处理异常: {e}\n{traceback.format_exc()}")
        return Response(status_code=500, content=f"error: {e}")


# ============================================================================
# 智能机器人后台处理 + dedup (一期重做: 避免 Dify 慢调用导致 5s 超时重试)
# ============================================================================

async def _process_bot_message_background(
    msg: dict,
    from_user: str,
    msg_type: str,
    effective_media_type: str,
    content: str,
    msg_id: str,
    response_url: str,
    wechat_media_ref: str,
    wechat_media_kind: str,
    timestamp: str,
    nonce: str,
    encoding_aes_key: str,
    kf_token: str,
):
    """后台异步处理 bot 消息: 调 AI workflow → 拼 markdown → 推 response_url。

    注: Dify 工作流跑 30-50s, 必须异步处理, 否则智能机器人 callback 5s 超时
    会触发 WeChat 重试, 导致同一个 response_url 被推多次 (errcode 60140)。

    媒体编排:
        - wechat_media_kind="url"        → httpx.GET(wechat_media_ref) 拿 bytes
        - wechat_media_kind="media_id"   → WeChatService.download_media 拿 bytes
    """
    import json as _json
    import httpx as _httpx
    try:
        # 0) 一期增强: image / voice 媒体编排
        # 智能机器人 image (url 路径): 直接用微信 CDN URL 走 Dify remote_url 模式 (跳过上传)
        # 客服 kf image / 智能机器人 voice (media_id 路径): 下载后上传 Dify 走 local_file 模式
        # dify.py 优先 file_image_url > file_image_id
        dify_file_image_url = ""
        dify_file_image_id = ""
        dify_file_voice_id = ""
        if wechat_media_ref:
            try:
                media_bytes: bytes = b""
                if wechat_media_kind == "url":
                    # 智能机器人 image: 不下载!直接用 CDN URL 喂 Dify remote_url 模式
                    # 仅 voice 需要先下载(转码可能要考虑), 暂不在 url 路径做 voice
                    if effective_media_type == "image":
                        dify_file_image_url = wechat_media_ref
                        logger.info(
                            f"[BOT BG] image 走 remote_url 模式: msgid={msg_id}, "
                            f"url={wechat_media_ref[:60]}..."
                        )
                    else:
                        # voice url 路径暂未用, 走通用下载
                        async with _httpx.AsyncClient(timeout=30.0) as ac:
                            r = await ac.get(wechat_media_ref)
                            r.raise_for_status()
                            media_bytes = r.content
                elif wechat_media_kind == "media_id":
                    # 客服 kf / voice: 调 WeChatService.download_media
                    svc = WeChatService()
                    media_bytes = await svc.download_media(wechat_media_ref)
                else:
                    raise RuntimeError(f"未知的 wechat_media_kind: {wechat_media_kind}")

                if effective_media_type == "image" and not dify_file_image_url:
                    # 仅 media_id 路径需要上传 (url 路径已用 remote_url)
                    dify_file_image_id = await _upload_to_dify_file_store(
                        media_bytes, wechat_media_ref, "image",
                    )
                    logger.info(
                        f"[BOT BG] image 上传 Dify 成功: msgid={msg_id}, "
                        f"kind={wechat_media_kind}, dify_file_id={dify_file_image_id}, "
                        f"size={len(media_bytes)}B"
                    )
                elif effective_media_type == "voice":
                    # voice 当前只支持 media_id 路径
                    dify_file_voice_id = await _upload_to_dify_file_store(
                        media_bytes, wechat_media_ref, "audio",
                    )
                    logger.info(
                        f"[BOT BG] voice 上传 Dify 成功: msgid={msg_id}, "
                        f"dify_file_id={dify_file_voice_id}, size={len(media_bytes)}B"
                    )
            except Exception as e:
                logger.error(f"[BOT BG] 媒体编排失败: msgid={msg_id}, {e}")

        # 1) 调 AI workflow
        # 支持的 msgtype: text / image / voice / mixed (图文混合, content + media_ref 都有)
        if msg_type not in ("text", "image", "voice", "mixed") or (
            msg_type in ("text", "mixed") and not content and not wechat_media_ref
        ):
            reply_text = (
                "收到不支持的消息类型"
                if msg_type not in ("text", "image", "voice", "mixed")
                else "收到空消息"
            )
        else:
            input_data: Dict[str, Any] = {"user_id": from_user}
            if content:
                input_data["text"] = content
            if dify_file_image_url:
                input_data["file_image_url"] = dify_file_image_url
            elif dify_file_image_id:
                input_data["file_image_id"] = dify_file_image_id
            if dify_file_voice_id:
                input_data["file_voice_id"] = dify_file_voice_id
            if "text" not in input_data:
                # image/voice 没附文本时给个提示, 让 LLM 知道要看图/听音
                input_data["text"] = (
                    "[用户发了一张图片]" if msg_type == "image"
                    else "[用户发了一段语音]" if msg_type == "voice"
                    else ""
                )

            ai = get_ai_service()
            try:
                wf = await ai.run_workflow(input_data, user_id=from_user)
                logger.info(
                    f"[BOT BG] AI 返回, msgid={msg_id}, 键="
                    f"{list(wf.keys()) if isinstance(wf, dict) else type(wf).__name__}"
                )
                reply_text = compose_multimodal_markdown(wf)
            except Exception as e:
                logger.error(f"[BOT BG] AI 失败: msgid={msg_id}, {e}")
                reply_text = f"AI 处理失败: {e}"

        if not reply_text:
            reply_text = "（AI 未返回内容）"
        logger.info(
            f"[BOT BG] msgid={msg_id}, reply_len={len(reply_text)}, "
            f"content[:80]={reply_text[:80]}"
        )

        # 2) 推 response_url (msgtype 必须 markdown)
        if response_url:
            payload = {"msgtype": "markdown", "markdown": {"content": reply_text}}
            try:
                async with _httpx.AsyncClient(timeout=30.0) as ac:
                    r = await ac.post(
                        response_url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )
                try:
                    errcode = _json.loads(r.text).get("errcode")
                except _json.JSONDecodeError:
                    errcode = None
                logger.info(
                    f"[BOT BG] 异步推送: msgid={msg_id}, "
                    f"HTTP {r.status_code} errcode={errcode}"
                )
            except Exception as e:
                logger.error(f"[BOT BG] 异步推送异常: msgid={msg_id}, {e}")
    except Exception as e:
        import traceback
        logger.error(f"[BOT BG] 后台任务异常: msgid={msg_id}, {e}\n{traceback.format_exc()}")


def _build_bot_sync_envelope(svc, reply_text: str, timestamp: str, nonce: str) -> str:
    """构造智能机器人同步响应 envelope (加密 JSON 字符串)。

    智能机器人 aibot/response 接口文档: 加密 JSON 信封 (encrypt + msgsignature + timestamp + nonce),
    内部明文是 {"msgtype": "markdown", "markdown": {"content": ...}}。
    """
    import json as _json
    try:
        envelope_xml = WeChatService.encrypt_message_custom(
            reply_xml=_json.dumps(
                {"msgtype": "markdown", "markdown": {"content": reply_text}},
                ensure_ascii=False,
            ),
            encoding_aes_key=svc.config.kf_encoding_aes_key,
            corp_id="",
            timestamp=timestamp,
            nonce=nonce,
            token=svc.config.kf_token,
        )
        env_root = ET.fromstring(envelope_xml)
        return _json.dumps({
            "encrypt": env_root.findtext("Encrypt"),
            "msgsignature": env_root.findtext("MsgSignature"),
            "timestamp": timestamp,
            "nonce": nonce,
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[BOT] 同步 envelope 构建失败: {e}")
        return "{}"


async def _upload_to_dify_file_store(
    content: bytes,
    wechat_media_ref: str,
    file_type: str,
) -> str:
    """把微信临时素材字节流上传到 Dify 文件库, 拿到 Dify upload_file_id。

    Dify workflow inputs file 型字段需要:
        [{"type": "image", "transfer_method": "local_file", "upload_file_id": "<dify_file_id>"}]
    dify.py run_workflow 会用 client.file_ref(dify_file_id, "image") 构造上述结构,
    所以这里只负责把微信的临时素材字节流推上 Dify 拿到 file_id 即可。

    Args:
        content: 微信临时素材的 bytes
        wechat_media_ref: 微信 media_id 或 url 字符串 (仅用于生成可读文件名)
        file_type: "image" | "audio" (Dify file_ref 的 type 字段)

    Returns:
        Dify upload_file_id (UUID 字符串), 用于喂给 dify.py run_workflow

    Raises:
        RuntimeError: 当前 AI 后端不是 Dify (无 client.upload_file) 或 Dify 上传失败
    """
    ai = get_ai_service()
    client = getattr(ai, "client", None)
    if client is None or not hasattr(client, "upload_file"):
        raise RuntimeError(
            f"当前 AI 后端 ({type(ai).__name__}) 不支持文件上传, "
            f"image/voice 消息转发无法工作"
        )

    # 文件名: 兼容 media_id (hex) 和 url (https://... 路径段) 两种
    ext_map = {"image": "jpg", "audio": "amr"}
    ext = ext_map.get(file_type, "bin")
    if "://" in wechat_media_ref:
        # URL: 取最后一段路径, 去掉 query/fragment
        from urllib.parse import urlparse
        path = urlparse(wechat_media_ref).path
        slug = path.rsplit("/", 1)[-1] or "file"
        slug = slug[:20]  # 截短避免文件名过长
        filename = f"wechat_{file_type}_{slug}.{ext}"
    else:
        # media_id: 看起来是 hex 串
        filename = f"wechat_{file_type}_{wechat_media_ref[:12]}.{ext}"
    mime_map = {
        "image": "image/jpeg",
        "audio": "audio/amr",
    }
    content_type = mime_map.get(file_type, "application/octet-stream")

    file_id = await client.upload_file(
        filename=filename,
        content=content,
        content_type=content_type,
    )
    return file_id
