"""Coze API集成服务"""
import asyncio
from typing import Dict, Any, Optional
import httpx
import aiofiles
import logging

from pydantic_core.core_schema import dataclass_args_schema

from app.core.config import settings
from app.core.exceptions import CozeAPIError
from app.services.wechat import WeChatService
from app.services.media import MediaService
from app.services.multimodal import extract_multimodal_payload

# Coze SDK
from cozepy import Coze, TokenAuth

logger = logging.getLogger(__name__)


def _coze_base_url() -> str:
    """根据 .env 的 COZE_API_BASE_URL 返回 Coze API 根地址。

    支持的值：
      - "https://api.coze.cn"  → 国内站
      - "https://api.coze.com"  → 海外站
      - "api.coze.cn" / "api.coze.com"（缺 scheme 时自动补 https://）
      - 带或不带尾部斜杠都会 strip
    """
    raw = (settings.coze.api_base_url or "https://api.coze.cn").strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw.rstrip("/")


class CozeService:
    """Coze智能体服务"""

    def __init__(self):
        # HTTP客户端用于工作流调用和文件上传
        self.client = httpx.AsyncClient(timeout=settings.coze.workflow_timeout)

        # Coze SDK客户端（保留以防需要其他SDK功能）
        # base_url 跟随 settings.coze.api_base_url，海外/国内站都兼容
        self.coze_client = Coze(
            auth=TokenAuth(token=settings.coze.api_token.get_secret_value()),
            base_url=_coze_base_url()
        )

        self.wechat_service = WeChatService()
        self.media_service = MediaService(self.wechat_service)

    async def trigger_workflow(self, input_data, user_id: str = "wechat_user") -> Dict[str, Any]:
        """触发Coze工作流 - 使用新的API方式

        Args:
            input_data: 输入数据，支持多种格式
            user_id: 用户ID，默认为 wechat_user

        Returns:
            工作流执行结果
        """
        return await self.run_workflow(input_data, user_id)

    async def run_workflow(self, input_data: Dict[str, Any], user_id: str = "wechat_user", conversation_id: Optional[str] = None) -> Dict[str, Any]:
        """运行Coze工作流 - 使用 stream_run API（SSE 流式响应）

        Args:
            input_data: 兼容旧字段，目前**不传给新工作流**（新工作流用空 parameters），
                        保留参数签名以便将来再扩展
            user_id: 用户ID，目前**不传给新工作流**
            conversation_id: 兼容 DifyService 同形接口; Coze workflow 模式无会话续接概念, 忽略

        Returns:
            工作流执行结果，统一为下游可识别的格式：
              - 若 stream 收到 done 事件，其 data 直接透传
              - 否则聚合 message/answer 事件为 {"reply_content": {"msgtype": "text", "text": {"content": "..."}}}
        """
        if not settings.coze.bot_id:
            raise CozeAPIError("Bot ID未配置，请设置COZE_BOT_ID环境变量")

        import json

        url = f"{_coze_base_url()}/v1/workflow/stream_run"
        headers = {
            "Authorization": f"Bearer {settings.coze.api_token.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        # 当前 Coze 工作流契约：parameters 为空对象
        payload = {
            "workflow_id": settings.coze.bot_id,
            "parameters": {},
        }

        logger.info(f"使用 stream_run API 调用工作流: workflow_id={settings.coze.bot_id}, base={_coze_base_url()}")
        logger.info(f"发送工作流请求: {payload}")

        try:
            async with self.client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()

                events: list = []
                answer_text = ""
                # 一期新增: 多模态字段聚合。Coze 工作流可以在"结束节点"自定义输出
                # JSON, 例如 {"output": "...", "images": [...], "videos": [...], "files": [...]}
                # 这里把多个 message 事件里的同名数组去重合并。
                images_list: list = []
                videos_list: list = []
                files_list: list = []
                done_data = None
                # 关键修复：Coze 把事件类型放在 SSE 的 `event:` 行里（不在 data JSON 里）
                # 标准 SSE 格式：event: <type>\ndata: <payload>\n\n
                # 需要维护当前事件的 type + data 状态，遇到空行才提交
                cur_evt_type: str = ""
                cur_evt_id: str = ""

                def _commit():
                    """提交当前累积的事件"""
                    nonlocal cur_evt_type, cur_evt_id, answer_text, done_data, events
                    nonlocal images_list, videos_list, files_list
                    if not cur_data_str:
                        return
                    if cur_data_str == "[DONE]":
                        return
                    try:
                        data_obj = json.loads(cur_data_str)
                    except json.JSONDecodeError:
                        logger.warning(f"SSE data 无法解析: {cur_data_str[:120]}")
                        return
                    events.append({"event": cur_evt_type, "id": cur_evt_id, "data": data_obj})

                    evt_type = cur_evt_type.lower() if cur_evt_type else ""
                    evt_data = data_obj if isinstance(data_obj, dict) else {}

                    # 每个 SSE 事件用 debug 级别, 默认不打印, 排错时打开
                    logger.debug(f"SSE event: type={cur_evt_type!r}, data_keys={list(evt_data.keys()) if isinstance(evt_data, dict) else None}")

                    if evt_type == "message":
                        content = evt_data.get("content")
                        if content:
                            # 一期重做: 把 content 解析成 dict 后调统一的 extract_multimodal_payload
                            # 该函数内部已支持真实工作流结构:
                            #   - 顶层 assistant_text (用户工作流实际形态)
                            #   - 顶层 media 数组 (按 type 分到 images/videos/files)
                            #   - 顶层 images/videos/files
                            #   - 嵌套 output 字段 (可能是 <think>{json}</think> 字符串)
                            if isinstance(content, str):
                                try:
                                    parsed_content = json.loads(content)
                                except (json.JSONDecodeError, ValueError):
                                    parsed_content = {"content": content}
                            elif isinstance(content, dict):
                                parsed_content = content
                            else:
                                parsed_content = {"content": str(content)}

                            if not isinstance(parsed_content, dict):
                                parsed_content = {"content": str(parsed_content)}

                            extracted = extract_multimodal_payload(parsed_content)

                            # text: 首个非空文本作为基础, 后续文本追加 (兼容 output 流式分段)
                            if extracted["text"]:
                                if not answer_text:
                                    answer_text = extracted["text"]
                                else:
                                    # 流式分段场景: 追加
                                    answer_text += extracted["text"]

                            # 多模态: 多事件去重合并
                            for url in extracted["images"]:
                                if url not in images_list:
                                    images_list.append(url)
                            for url in extracted["videos"]:
                                if url not in videos_list:
                                    videos_list.append(url)
                            for url in extracted["files"]:
                                if url not in files_list:
                                    files_list.append(url)
                    elif evt_type == "done":
                        done_data = evt_data
                    elif evt_type == "error":
                        logger.error(f"SSE error event: {data_obj}")
                        raise CozeAPIError(f"工作流执行错误: {data_obj}")
                    elif evt_type == "interrupt":
                        logger.info(f"工作流被打断（需要外部输入）: {evt_data}")

                cur_data_str: str = ""

                async for line in response.aiter_lines():
                    line = line.rstrip("\n\r")
                    if not line:
                        # 空行 = 事件分隔符，提交当前事件
                        _commit()
                        cur_evt_type = ""
                        cur_evt_id = ""
                        cur_data_str = ""
                        if done_data is not None:
                            break
                        continue
                    if line.startswith("event:"):
                        cur_evt_type = line[6:].strip()
                    elif line.startswith("data:"):
                        cur_data_str += line[5:].strip()
                    elif line.startswith("id:"):
                        cur_evt_id = line[3:].strip()
                    # 其他 SSE 字段（retry: 等）忽略

                # 流结束，若还有未提交的事件则提交
                if cur_data_str:
                    _commit()

                logger.info(
                    f"stream_run 收到 {len(events)} 个事件, answer 长度={len(answer_text)}, "
                    f"images={len(images_list)}, videos={len(videos_list)}, files={len(files_list)}, "
                    f"done_data={type(done_data).__name__}"
                )

                # 一期新增: 即使 answer_text 为空，只要工作流产出了多模态 URL，也照样返回
                # 因为 markdown 内嵌图片也能"无文本有图"
                has_text = bool(answer_text)
                has_multimodal = bool(images_list or videos_list or files_list)

                if has_text or has_multimodal:
                    result: Dict[str, Any] = {
                        # 兼容旧字段 (下游 process_single_message / bot_callback_handler 仍按 reply_content 解)
                        "reply_content": {
                            "msgtype": "text",
                            "text": {"content": answer_text},
                        },
                        # 一期新增: 顶层多模态字段，下游用 compose_multimodal_markdown 拼 markdown
                        "text": answer_text,
                        "images": images_list,
                        "videos": videos_list,
                        "files": files_list,
                    }
                    return result

                # 兜底：如果没有任何 message 事件聚合到内容，再用 done_data
                # 但 done_data 通常只是 SSE meta (debug_url)，不是工作流输出
                # 仍补上 text/images/videos/files 空字段, 保证下游结构一致
                if isinstance(done_data, dict):
                    return {
                        **done_data,
                        "text": "",
                        "images": [],
                        "videos": [],
                        "files": [],
                    }
                if done_data is not None:
                    return {
                        "data": done_data,
                        "text": "",
                        "images": [],
                        "videos": [],
                        "files": [],
                    }

                return {
                    "reply_content": {
                        "msgtype": "text",
                        "text": {"content": ""},
                    },
                    "text": "",
                    "images": [],
                    "videos": [],
                    "files": [],
                }

        except httpx.HTTPStatusError as e:
            err_body = ""
            try:
                err_body = e.response.text[:500]
            except Exception:
                pass
            logger.error(f"工作流HTTP请求失败，状态码: {e.response.status_code}, 响应: {err_body}")
            raise CozeAPIError(f"工作流API请求失败: HTTP {e.response.status_code}")
        except httpx.RequestError as e:
            logger.error(f"工作流网络请求失败: {e}")
            raise CozeAPIError(f"工作流网络请求异常: {str(e)}")
        except Exception as e:
            logger.error(f"工作流调用异常: {e}")
            raise CozeAPIError(f"运行工作流异常: {str(e)}")

    async def upload_file(self, file_content: bytes, file_name: str) -> str:
        """上传文件到Coze并返回文件ID
        
        Args:
            file_content: 文件内容（字节）
            file_name: 文件名
            
        Returns:
            文件ID（file_id）
        """
        url = f"{_coze_base_url()}/v1/files/upload"

        # 构建multipart/form-data请求
        files = {
            'file': (file_name, file_content, 'application/octet-stream')
        }

        headers = {
            "Authorization": f"Bearer {settings.coze.api_token.get_secret_value()}"
        }

        try:
            response = await self.client.post(url, files=files, headers=headers)
            result = response.json()

            if result.get("code") == 0:
                file_data = result.get("data", {})
                # 返回文件ID，优先使用id字段，如果没有则使用file_id字段
                file_id = file_data.get("file_id") or file_data.get("id") or file_data.get("url", "")
                if not file_id:
                    # 如果都没有，记录警告但返回空字符串
                    logger.warning(f"文件上传响应中未找到文件ID，响应数据: {file_data}")
                return file_id
            else:
                raise CozeAPIError(
                    f"文件上传失败: {result}",
                    code=result.get("code"),
                    details=result
                )

        except Exception as e:
            if isinstance(e, CozeAPIError):
                raise
            raise CozeAPIError(f"上传文件异常: {str(e)}")

    async def process_wechat_message(self, wechat_msg: Dict[str, Any]) -> Dict[str, str]:
        """处理微信消息并转换为简化输入格式

        Args:
            wechat_msg: 微信消息原始格式

        Returns:
            简化输入格式: {'text': str, 'image_url': str, 'voice_url': str}
        """
        result = {
            'text': '',
            'image_url': '',
            'voice_url': ''
        }

        msg_type = wechat_msg.get('msgtype')

        if msg_type == 'text':
            # 处理文本消息
            result['text'] = wechat_msg.get('text', {}).get('content', '')

        elif msg_type == 'image':
            # 处理图片消息
            media_id = wechat_msg.get('image', {}).get('media_id')
            if media_id:
                # 下载图片文件
                image_content = await self.wechat_service.download_media(media_id)
                # 上传到Coze并获取URL
                image_url = await self.upload_file(image_content, f"wechat_image_{media_id}.jpg")
                result['image_url'] = image_url

        elif msg_type == 'voice':
            # 处理语音消息
            media_id = wechat_msg.get('voice', {}).get('media_id')
            if media_id:
                # 使用MediaService下载并转换音频格式
                media_info = await self.media_service.download_and_process_media(media_id, 'voice')

                if media_info.get('error'):
                    # 如果处理失败，使用原始AMR格式
                    voice_content = await self.wechat_service.download_media(media_id)
                    voice_url = await self.upload_file(voice_content, f"wechat_voice_{media_id}.wav")
                    result['voice_url'] = voice_url
                else:
                    # 使用转换后的文件
                    if media_info.get('converted') and media_info.get('wav_path'):
                        # 读取转换后的WAV文件
                        async with aiofiles.open(media_info['wav_path'], 'rb') as f:
                            wav_content = await f.read()
                        voice_url = await self.upload_file(wav_content, f"wechat_voice_{media_id}.wav")
                        result['voice_url'] = voice_url
                    else:
                        # 没有转换，使用原始文件
                        voice_content = await self.wechat_service.download_media(media_id)
                        voice_url = await self.upload_file(voice_content, f"wechat_voice_{media_id}.wav")
                        result['voice_url'] = voice_url

        else:
            # 不支持的消息类型，返回提示文本
            if msg_type:
                result['text'] = f"暂不支持处理 {msg_type} 类型的消息"
            else:
                result['text'] = "无法识别的消息类型"

        return result

    async def close(self):
        """关闭HTTP客户端"""
        await self.client.aclose()
        await self.wechat_service.close()