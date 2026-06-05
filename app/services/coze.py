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

# Coze SDK
from cozepy import COZE_CN_BASE_URL, Coze, TokenAuth

logger = logging.getLogger(__name__)


class CozeService:
    """Coze智能体服务"""

    def __init__(self):
        # HTTP客户端用于工作流调用和文件上传
        self.client = httpx.AsyncClient(timeout=settings.coze.workflow_timeout)

        # Coze SDK客户端（保留以防需要其他SDK功能）
        self.coze_client = Coze(
            auth=TokenAuth(token=settings.coze.api_token.get_secret_value()),
            base_url=COZE_CN_BASE_URL
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

    async def run_workflow(self, input_data: Dict[str, Any], user_id: str = "wechat_user") -> Dict[str, Any]:
        """运行Coze工作流 - 使用run API

        Args:
            input_data: 输入数据，支持多种格式：
                - {'text': str, 'file_image_id': str, 'file_voice_id': str} (简化格式，会转换为parameters格式)
                - 其他自定义格式
            user_id: 用户ID，默认为 wechat_user

        Returns:
            工作流执行结果

        Note:
            使用run API，直接返回完整响应。
            简化格式会转换为Coze工作流parameters格式：
            - 文本: {"text": "消息内容"}
            - 图片: {"file_image_id": "{\"file_id\":\"文件ID\"}"}
            - 语音: {"file_voice_id": "{\"file_id\":\"文件ID\"}"}
        """
        if not settings.coze.bot_id:
            raise CozeAPIError("Bot ID未配置，请设置COZE_BOT_ID环境变量")

        # 处理不同的输入格式
        if isinstance(input_data, dict):
            # 检查是否为简化输入格式：{'text': str, 'file_image_id': str, 'file_voice_id': str}
            if any(key in input_data for key in ['text', 'file_image_id', 'file_voice_id']):
                # 构建Coze工作流所需的parameters格式
                workflow_input = {}
                # 处理文本
                if input_data.get('text'):
                    workflow_input['text'] = input_data['text']
                    logger.info(f"设置文本参数: {input_data['text']}")

                # 处理图片 - 使用 image 参数，值为JSON字符串格式
                if input_data.get('file_image_id'):
                    import json
                    workflow_input['file_image_id'] = json.dumps({"file_id": input_data['file_image_id']}, ensure_ascii=False)
                    logger.info(f"设置图片参数 image: {workflow_input['file_image_id']}")

                # 处理语音 - 使用 voice 参数，值为JSON字符串格式
                if input_data.get('file_voice_id'):
                    import json
                    workflow_input['file_voice_id'] = json.dumps({"file_id": input_data['file_voice_id']}, ensure_ascii=False)
                    logger.info(f"设置语音参数 voice: {workflow_input['file_voice_id']}")

                workflow_input['user_id'] = user_id
                # 如果没有任何有效参数，提供默认文本
                if not workflow_input:
                    workflow_input['text'] = '收到您的消息'
                    logger.info("使用默认文本参数")
            else:
                # 直接使用提供的输入数据
                workflow_input = input_data
        else:
            workflow_input = input_data

        try:
            logger.info(f"使用run API调用工作流: workflow_id={settings.coze.bot_id}")
            logger.info(f"workflow_input: {workflow_input}")
            # 构建API请求
            import json
            url = "https://api.coze.cn/v1/workflow/run"
            headers = {
                "Authorization": f"Bearer {settings.coze.api_token.get_secret_value()}",
                "Content-Type": "application/json"
            }
            payload = {
                "workflow_id": settings.coze.bot_id,
                "parameters": workflow_input
            }

            logger.info(f"发送工作流请求: {payload}")

            # 发送HTTP请求（使用json参数发送JSON数据）
            response = await self.client.post(url, headers=headers, json=payload)
            response.raise_for_status()

            result_data = response.json()
            logger.info(f"工作流返回数据: {result_data}")

            # 处理run API的响应格式（直接返回完整结果）
            if isinstance(result_data, dict):
                logger.info("run API调用成功，返回字典格式数据")
                return result_data
            else:
                logger.warning(f"API返回数据类型异常: {type(result_data)}")
                return {"data": str(result_data) if result_data is not None else ""}

        except httpx.HTTPStatusError as e:
            logger.error(f"工作流HTTP请求失败，状态码: {e.response.status_code}, 响应: {e.response.text}")
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
        url = "https://api.coze.cn/v1/files/upload"

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