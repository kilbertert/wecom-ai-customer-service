"""媒体文件处理服务"""
import os
import uuid
import base64
import mimetypes
import asyncio
from typing import Dict, Any, Optional, Tuple
from pathlib import Path
import aiofiles
import io

try:
    from pydub import AudioSegment
    # 设置 FFmpeg 路径
    # 设置 FFmpeg 路径 (自动检测系统路径)
    import shutil
    import platform

    # 检测系统并设置FFmpeg路径
    system = platform.system().lower()

    if system == "windows":
        # Windows路径 (Chocolatey安装)
        ffmpeg_paths = [
            r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
            r"C:\ffmpeg\bin\ffmpeg.exe",
            "ffmpeg.exe"  # PATH环境变量中查找
        ]
        ffprobe_paths = [
            r"C:\ProgramData\chocolatey\bin\ffprobe.exe",
            r"C:\ffmpeg\bin\ffprobe.exe",
            "ffprobe.exe"
        ]
    else:
        # Linux/macOS路径
        ffmpeg_paths = [
            "/usr/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/opt/homebrew/bin/ffmpeg",  # macOS Homebrew
            "ffmpeg"  # PATH环境变量中查找
        ]
        ffprobe_paths = [
            "/usr/bin/ffprobe",
            "/usr/local/bin/ffprobe",
            "/opt/homebrew/bin/ffprobe",
            "ffprobe"
        ]

    # 查找可用的FFmpeg
    ffmpeg_path = None
    ffprobe_path = None

    for path in ffmpeg_paths:
        if shutil.which(path) or (system == "windows" and shutil.which(path)):
            ffmpeg_path = path
            break

    for path in ffprobe_paths:
        if shutil.which(path) or (system == "windows" and shutil.which(path)):
            ffprobe_path = path
            break

    if ffmpeg_path and ffprobe_path:
        AudioSegment.converter = ffmpeg_path
        AudioSegment.ffmpeg = ffmpeg_path
        AudioSegment.ffprobe = ffprobe_path
        PYDUB_AVAILABLE = True
    else:
        print("[WARNING] FFmpeg not found, audio processing will be disabled")
        PYDUB_AVAILABLE = False
except ImportError:
    PYDUB_AVAILABLE = False

from app.core.config import settings
from app.services.wechat import WeChatService


class MediaService:
    """媒体文件处理服务"""

    def __init__(self, wechat_service: WeChatService):
        self.wechat_service = wechat_service
        self.temp_dir = Path("./temp_media")
        self.temp_dir.mkdir(exist_ok=True)

    async def download_and_process_media(self, media_id: str, media_type: str) -> Dict[str, Any]:
        """下载并处理媒体文件"""
        try:
            # 下载媒体文件
            media_data = await self.wechat_service.download_media(media_id)

            if not media_data:
                return {"error": "下载失败", "media_id": media_id}

            # 确定文件类型和扩展名
            content_type, extension = self._guess_file_type(media_data, media_type)

            # 生成唯一文件名
            filename = f"{uuid.uuid4()}{extension}"

            # 保存临时文件
            file_path = self.temp_dir / filename
            async with aiofiles.open(file_path, 'wb') as f:
                await f.write(media_data)

            # 构建媒体信息
            media_info = {
                "media_id": media_id,
                "filename": filename,
                "file_path": str(file_path),
                "content_type": content_type,
                "size": len(media_data),
                "url": "",  # 可以后续设置CDN URL
                "local_path": str(file_path)
            }

            # 特殊处理
            if media_type == "image":
                media_info.update(await self._process_image(media_data, file_path))
            elif media_type == "voice":
                media_info.update(await self._process_voice(media_data, file_path))
            elif media_type == "video":
                media_info.update(await self._process_video(media_data, file_path))

            return media_info

        except Exception as e:
            return {
                "error": f"处理媒体文件失败: {str(e)}",
                "media_id": media_id
            }

    def _guess_file_type(self, data: bytes, media_type: str) -> Tuple[str, str]:
        """猜测文件类型和扩展名"""
        # 基于媒体类型确定默认扩展名
        type_mapping = {
            "image": ("image/jpeg", ".jpg"),
            "voice": ("audio/amr", ".amr"),
            "video": ("video/mp4", ".mp4"),
            "file": ("application/octet-stream", "")
        }

        content_type, extension = type_mapping.get(media_type, ("application/octet-stream", ""))

        # 尝试从数据头部识别
        if len(data) >= 4:
            # 检查文件签名
            if data.startswith(b'\xff\xd8\xff'):  # JPEG
                return "image/jpeg", ".jpg"
            elif data.startswith(b'\x89PNG'):  # PNG
                return "image/png", ".png"
            elif data.startswith(b'GIF8'):  # GIF
                return "image/gif", ".gif"
            elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':  # WebP
                return "image/webp", ".webp"
            elif data.startswith(b'AMR'):  # AMR音频
                return "audio/amr", ".amr"
            elif data.startswith(b'\x00\x00\x00'):  # MP4视频
                return "video/mp4", ".mp4"

        return content_type, extension

    async def _process_image(self, data: bytes, file_path: Path) -> Dict[str, Any]:
        """处理图片文件"""
        try:
            # 这里可以添加图片处理逻辑，如压缩、格式转换等
            # 目前返回基本信息
            return {
                "width": 0,  # 需要图像处理库获取
                "height": 0,
                "format": file_path.suffix[1:].upper(),
                "processed": True
            }
        except Exception as e:
            return {"processed": False, "error": str(e)}

    async def _process_voice(self, data: bytes, file_path: Path) -> Dict[str, Any]:
        """处理语音文件"""
        try:
            # 检查是否为AMR格式，如果是则转换为WAV
            if PYDUB_AVAILABLE and file_path.suffix.lower() == '.amr':
                wav_data = await self._convert_amr_to_wav(data)
                if wav_data:
                    # 生成新的WAV文件路径
                    wav_path = file_path.with_suffix('.wav')
                    async with aiofiles.open(wav_path, 'wb') as f:
                        await f.write(wav_data)

                    # 获取音频信息
                    audio_info = await self._get_audio_info(wav_data)

                    return {
                        "duration": audio_info.get("duration", 0),
                        "format": "WAV",
                        "original_format": "AMR",
                        "converted": True,
                        "wav_path": str(wav_path),
                        "processed": True
                    }

            # 非AMR格式或转换失败，返回原始信息
            audio_info = await self._get_audio_info(data) if PYDUB_AVAILABLE else {}
            return {
                "duration": audio_info.get("duration", 0),
                "format": file_path.suffix[1:].upper(),
                "converted": False,
                "processed": True
            }
        except Exception as e:
            return {"processed": False, "error": str(e)}

    async def _process_video(self, data: bytes, file_path: Path) -> Dict[str, Any]:
        """处理视频文件"""
        try:
            # 这里可以添加视频处理逻辑，如缩略图生成、格式转换等
            return {
                "duration": 0,  # 需要视频处理库获取
                "width": 0,
                "height": 0,
                "format": file_path.suffix[1:].upper(),
                "processed": True
            }
        except Exception as e:
            return {"processed": False, "error": str(e)}

    async def _convert_amr_to_wav(self, amr_data: bytes) -> Optional[bytes]:
        """将AMR音频转换为WAV格式"""
        if not PYDUB_AVAILABLE:
            print("pydub不可用，跳过音频转换")
            return None

        try:
            # 使用BytesIO创建内存文件对象
            amr_io = io.BytesIO(amr_data)
            amr_io.seek(0)

            # 加载AMR音频
            audio = AudioSegment.from_file(amr_io, format="amr")

            # 转换为WAV格式
            wav_io = io.BytesIO()
            audio.export(wav_io, format="wav")

            return wav_io.getvalue()

        except Exception as e:
            print(f"AMR转WAV失败: {str(e)}")
            return None

    async def _get_audio_info(self, audio_data: bytes) -> Dict[str, Any]:
        """获取音频文件信息"""
        if not PYDUB_AVAILABLE:
            return {}

        try:
            audio_io = io.BytesIO(audio_data)
            audio_io.seek(0)

            # 尝试自动检测格式
            audio = AudioSegment.from_file(audio_io)

            return {
                "duration": len(audio) / 1000.0,  # 毫秒转秒
                "channels": audio.channels,
                "sample_width": audio.sample_width,
                "frame_rate": audio.frame_rate
            }

        except Exception as e:
            print(f"获取音频信息失败: {str(e)}")
            return {}

    def get_media_url(self, media_id: str) -> Optional[str]:
        """获取媒体文件的URL"""
        # 这里可以实现CDN URL生成逻辑
        # 例如: return f"https://cdn.example.com/media/{media_id}"
        return None

    async def cleanup_temp_files(self, max_age_hours: int = 24):
        """清理临时文件"""
        try:
            import time
            current_time = time.time()

            for file_path in self.temp_dir.glob("*"):
                if file_path.is_file():
                    file_age = current_time - file_path.stat().st_mtime
                    if file_age > (max_age_hours * 3600):
                        file_path.unlink()

        except Exception as e:
            print(f"清理临时文件失败: {str(e)}")

    def encode_media_to_base64(self, file_path: str) -> Optional[str]:
        """将媒体文件编码为Base64"""
        try:
            with open(file_path, 'rb') as f:
                data = f.read()
                return base64.b64encode(data).decode('utf-8')
        except Exception as e:
            print(f"Base64编码失败: {str(e)}")
            return None

    async def get_media_info(self, media_id: str) -> Optional[Dict[str, Any]]:
        """获取媒体文件信息"""
        try:
            # 查找临时文件
            for file_path in self.temp_dir.glob(f"*{media_id}*"):
                if file_path.exists():
                    stat = file_path.stat()
                    return {
                        "media_id": media_id,
                        "file_path": str(file_path),
                        "size": stat.st_size,
                        "created_time": stat.st_ctime,
                        "exists": True
                    }

            return None

        except Exception as e:
            print(f"获取媒体信息失败: {str(e)}")
            return None

    def download_and_process_media_sync(self, media_id: str, media_type: str) -> Dict[str, Any]:
        """同步版本的媒体文件下载和处理"""
        """同步版本：下载并处理媒体文件"""
        try:
            # 创建新的事件循环来运行异步代码
            async def _async_process():
                return await self.download_and_process_media(media_id, media_type)

            # 在同步上下文中运行异步代码
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_async_process())
            finally:
                loop.close()

        except Exception as e:
            return {
                "error": f"处理媒体文件失败: {str(e)}",
                "media_id": media_id
            }
