"""多模态回复工具模块。

把 Dify 返回的复杂工作流结构
归一化成 markdown 文本，供客服 / 智能机器人两套链路复用。

一期 (markdown 内嵌图片) 路径:
    AI 工作流产出 {assistant_text, media, raw, ...} (用户真实结构)
        ↓
    extract_multimodal_payload() → {text, images, videos, files}
        ↓
    compose_multimodal_markdown() → "文本\n\n![](url)\n\n[视频](url)\n\n[文件](url)"
        ↓
    客服: send_message_simple(..., text=markdown)
    智能机器人: response_url POST {msgtype: markdown, markdown: {content: markdown}}

二期 (客服 native 多模态) 路径会扩展出:
    upload_multimodal(ai_payload) → media_id 列表 + 逐条 send_msg
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Dify 工作流 LLM 节点可能输出 <think>...</think> 思考块 (thinking-enabled 模型)
# 必须过滤掉，否则会污染最终回复文本
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


# ---------------------------------------------------------------------------
# 文本提取
# ---------------------------------------------------------------------------


def _strip_thinking(text: str) -> str:
    """过滤 <think>...</think> 思考块。

    工作流如果用了 thinking-enabled 模型, 会在 output 里泄露思考过程。
    必须 strip 掉再交给用户。
    """
    if not isinstance(text, str) or not text:
        return ""
    return _THINK_BLOCK_RE.sub("", text).strip()


# 图片扩展名 (用于扫描 text 里的裸图片 URL)
_IMAGE_EXTS = ("png", "jpg", "jpeg", "gif", "webp", "bmp", "svg")
# 匹配 text 中的裸图片 URL 并包装为 markdown image 语法
# 负向后行 (?<!\]\()  : 跳过已经在 ![..](..) 或 [..](..) 内的 URL, 避免嵌套
# 前瞻 (?=[\s,;.。、，;；!！?？]|$) : URL 必须以空白/标点/字符串结尾
_INLINE_IMAGE_URL_RE = re.compile(
    r"(?<!\]\()"
    r"(https?://[^\s]+?\.(?:" + "|".join(_IMAGE_EXTS) + r")"
    r"(?:[?#][^\s]*?)?)"
    r"(?=[\s,;.。、，;；!！?？]|$)",
    re.IGNORECASE,
)


def _convert_inline_image_urls(text: str, image_alt: str = "图片") -> str:
    """扫描 text, 把裸的 ``.png/.jpg/.jpeg/.gif/.webp/.bmp/.svg`` URL 转成 markdown 图片语法。

    解决 Dify workflow 把图片 URL 嵌在 text 字符串里返回的场景 — 不包装的话
    WeChat 把 URL 当成普通链接显示, 而不是渲染图片。

    已经包装在 ``![alt](url)`` 内的 URL 不会被重复包装 (用负向后行 ``](`` 排除)。

    Examples:
        >>> _convert_inline_image_urls("看 https://x.com/a.png 完")
        '看 ![图片](https://x.com/a.png) 完'
        >>> _convert_inline_image_urls("![](https://x.com/a.png)")
        '![](https://x.com/a.png)'
    """
    if not isinstance(text, str) or not text:
        return text

    def _wrap(match: "re.Match[str]") -> str:
        url = match.group(1)
        return f"![{image_alt}]({url})"

    return _INLINE_IMAGE_URL_RE.sub(_wrap, text)


def _extract_text_from_nested_json(value: str) -> str:
    """尝试把字符串当作 JSON 二次解析, 提取内部的 text 字段。

    工作流 LLM 节点可能输出形如 ``<think>...</think>{"text": "...", "media": [...]}` 格式,
    过滤 think 后剩下的可能是合法 JSON, 里面才是真正的回复。
    """
    if not isinstance(value, str):
        return ""
    cleaned = _strip_thinking(value)
    if not cleaned:
        return ""
    stripped = cleaned.strip()
    if stripped.startswith("{"):
        try:
            inner = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return cleaned
        if isinstance(inner, dict):
            # 二次解析后再走一遍文本提取
            return _extract_text_from_workflow_result(inner)
    return cleaned


def _extract_text_from_workflow_result(wf: Dict[str, Any]) -> str:
    """从工作流返回里提取纯文本，兼容多种字段命名。

    优先级 (与真实工作流结构对齐):
        1. wf["assistant_text"]            ← 顶层纯文本回答 (用户工作流实际形态)
        2. wf["text"]
        3. wf["content"]                   (Dify 归一化字段)
        4. wf["output"]                    ← LLM 节点输出, 可能是嵌套 JSON 字符串
        5. wf["data"] (字符串)
    """
    if not isinstance(wf, dict):
        return ""

    # 1. assistant_text (用户工作流的真实文本源)
    v = wf.get("assistant_text")
    if isinstance(v, str) and v.strip():
        return _strip_thinking(v)

    # 2. text
    v = wf.get("text")
    if isinstance(v, str) and v.strip():
        return _strip_thinking(v)

    # 3. content (Dify 归一化字段)
    v = wf.get("content")
    if isinstance(v, str) and v.strip():
        return _strip_thinking(v)

    # 6. output (可能是嵌套 JSON 字符串, 二次解析)
    v = wf.get("output")
    if isinstance(v, str) and v.strip():
        return _extract_text_from_nested_json(v)

    # 7. data (字符串)
    data = wf.get("data")
    if isinstance(data, str) and data.strip():
        return _strip_thinking(data)

    return ""


# ---------------------------------------------------------------------------
# 多模态 URL 提取
# ---------------------------------------------------------------------------


def _split_media(value: Any) -> Tuple[List[str], List[str], List[str]]:
    """从 media 数组(或 urls 数组)按 type 分到 (images, videos, files)。

    接受:
        - list[str]                  → 全部当 image
        - list[dict] [{type, url, ...}] → 按 type 分类
        - 单个 str                  → 当 image

    返回三个独立列表。
    """
    images: List[str] = []
    videos: List[str] = []
    files: List[str] = []

    if value is None:
        return images, videos, files
    if isinstance(value, str):
        v = value.strip()
        if v:
            images.append(v)
        return images, videos, files
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                v = item.strip()
                if v:
                    images.append(v)
            elif isinstance(item, dict):
                url = (
                    item.get("url")
                    or item.get("file_url")
                    or item.get("image_url")
                    or item.get("video_url")
                )
                if not isinstance(url, str) or not url.strip():
                    continue
                url = url.strip()
                type_ = (item.get("type") or "").lower()
                if type_ == "video":
                    videos.append(url)
                elif type_ in ("file", "document"):
                    files.append(url)
                else:
                    # 默认 image (无 type 或 type=image 都归这里)
                    images.append(url)
    return images, videos, files


def _collect_candidate_outputs(wf: Dict[str, Any]) -> List[Any]:
    """递归收集 wf 里所有可能的 output/outputs 字段值, 供后续解析。

    用户真实工作流结构里 output 可能位于:
        wf["output"]                          ← 旧结构 (遗留)
        wf["outputs"]                         ← Dify / 变体
        wf["raw"]["output"]                   ← 嵌套层
        wf["raw"]["outputs"]                  ← 嵌套层 (可能是 dict)
        wf["raw"]["data"]["output"]           ← run API 形态
        wf["raw"]["data"]["outputs"]          ← run API 形态 (可能是 dict)
            wf["raw"]["data"]["outputs"]["output"]  ← 用户真实结构!
    """
    candidates: List[Any] = []
    if not isinstance(wf, dict):
        return candidates

    def _walk(obj: Any) -> None:
        """递归找 output/outputs 字段"""
        if isinstance(obj, dict):
            for key in ("output", "outputs"):
                v = obj.get(key)
                if v is not None:
                    candidates.append(v)
            # 递归所有值
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(wf)
    return candidates


def _extract_multimodal_from_wf(
    wf: Dict[str, Any],
) -> Tuple[List[str], List[str], List[str]]:
    """从工作流结果 dict 提取多模态 URL, 返回 (images, videos, files) 元组。

    支持来源:
        - 顶层 wf["media"]    (用户工作流实际形态: 按 type 分)
        - 顶层 wf["images"] / wf["videos"] / wf["files"]  (简化形态)
        - 嵌套 wf["output"] / wf["raw"]["output"] / wf["raw"]["data"]["outputs"]["output"]
          解析后的 JSON 里的 media/images
    """
    if not isinstance(wf, dict):
        return [], [], []

    images: List[str] = []
    videos: List[str] = []
    files: List[str] = []

    # 1. 顶层 media 数组 (按 type 分)
    if "media" in wf:
        imgs, vids, fls = _split_media(wf["media"])
        images.extend(imgs)
        videos.extend(vids)
        files.extend(fls)

    # 2. 顶层 images/videos/files (字段名本身就是分桶语义, 裸 url 直接 append)
    for url in _coerce_url_list(wf.get("images")):
        if url not in images:
            images.append(url)
    for url in _coerce_url_list(wf.get("videos")):
        if url not in videos:
            videos.append(url)
    for url in _coerce_url_list(wf.get("files")):
        if url not in files:
            files.append(url)

    # 3. 递归查找嵌套 output 字段 (适配用户真实结构 raw.data.outputs.output)
    for candidate in _collect_candidate_outputs(wf):
        if isinstance(candidate, str) and candidate.strip():
            cleaned = _strip_thinking(candidate).strip()
            if cleaned.startswith("{"):
                try:
                    inner = json.loads(cleaned)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(inner, dict):
                    continue
                if "media" in inner:
                    imgs, vids, fls = _split_media(inner["media"])
                    for u in imgs:
                        if u not in images:
                            images.append(u)
                    for u in vids:
                        if u not in videos:
                            videos.append(u)
                    for u in fls:
                        if u not in files:
                            files.append(u)
                if "images" in inner:
                    for url in _coerce_url_list(inner["images"]):
                        if url not in images:
                            images.append(url)
                if "videos" in inner:
                    for url in _coerce_url_list(inner["videos"]):
                        if url not in videos:
                            videos.append(url)
                if "files" in inner:
                    for url in _coerce_url_list(inner["files"]):
                        if url not in files:
                            files.append(url)
        elif isinstance(candidate, dict):
            # outputs 是 dict 形态 (Dify 风格)
            if "media" in candidate:
                imgs, vids, fls = _split_media(candidate["media"])
                for u in imgs:
                    if u not in images:
                        images.append(u)
                for u in vids:
                    if u not in videos:
                        videos.append(u)
                for u in fls:
                    if u not in files:
                        files.append(u)

    # 去重保序
    images = list(dict.fromkeys(images))
    videos = list(dict.fromkeys(videos))
    files = list(dict.fromkeys(files))
    return images, videos, files


# ---------------------------------------------------------------------------
# 对外主接口 (保留旧签名, 行为增强)
# ---------------------------------------------------------------------------


def extract_multimodal_payload(wf: Dict[str, Any]) -> Dict[str, Any]:
    """从 AI 工作流结果里提取统一的多模态字段。

    Returns:
        {
            "text":   str,         # 文本回答
            "images": List[str],   # 图片 URL
            "videos": List[str],   # 视频 URL
            "files":  List[str],   # 文件 URL
        }

    任一字段缺失时返回空字符串 / 空列表，不抛异常。
    """
    if not isinstance(wf, dict):
        return {"text": "", "images": [], "videos": [], "files": []}
    text = _extract_text_from_workflow_result(wf)
    images, videos, files = _extract_multimodal_from_wf(wf)

    return {
        "text": text,
        "images": images,
        "videos": videos,
        "files": files,
    }


def compose_multimodal_markdown(
    wf: Dict[str, Any],
    *,
    image_alt: str = "图片",
) -> str:
    """把工作流结果转成 markdown 文本，供智能机器人 / 客服用。

    格式约定:
        <text>

        ![图片](url1)

        ![图片](url2)

        [视频](video_url)

        [文件](file_url)

    Args:
        wf: AI 工作流结果字典
        image_alt: markdown 图片 alt 文本 (默认 "图片")

    Returns:
        拼接后的 markdown 字符串。无任何内容时返回空字符串。
    """
    payload = extract_multimodal_payload(wf)

    # 处理 text 里的裸图片 URL (Dify 常见场景: 把图链嵌在 text 字符串里)
    # 包装后, 同步剔除 images 数组中已在 text 里出现过的, 避免末尾重复附加
    if payload["text"]:
        wrapped_text = _convert_inline_image_urls(payload["text"], image_alt=image_alt)
        inline_urls = set(_INLINE_IMAGE_URL_RE.findall(payload["text"]))
        payload["images"] = [u for u in payload["images"] if u not in inline_urls]
    else:
        wrapped_text = ""

    parts: List[str] = []
    if wrapped_text:
        parts.append(wrapped_text)

    for url in payload["images"]:
        parts.append(f"\n\n![{image_alt}]({url})")
    for url in payload["videos"]:
        parts.append(f"\n\n[视频]({url})")
    for url in payload["files"]:
        parts.append(f"\n\n[文件]({url})")

    if not parts:
        return ""

    # 第一个 part 是文本头（不带前导 \n\n），其余 part 自带 \n\n 前缀
    if len(parts) == 1:
        return parts[0]
    result = parts[0] + "".join(parts[1:])
    logger.debug(
        "compose_multimodal_markdown: text_len=%d, images=%d, videos=%d, files=%d",
        len(payload["text"]),
        len(payload["images"]),
        len(payload["videos"]),
        len(payload["files"]),
    )
    return result


def has_multimodal_payload(wf: Dict[str, Any]) -> bool:
    """判断工作流结果是否包含多模态 URL。"""
    p = extract_multimodal_payload(wf)
    return bool(p["images"] or p["videos"] or p["files"])


# ---------------------------------------------------------------------------
# 兼容旧 API: _coerce_url_list (dify.py 多模态字段提取在用, 保留)
# ---------------------------------------------------------------------------


def _coerce_url_list(value: Any) -> List[str]:
    """把可能是 list[str] / 单个 str / None 都归一化成 list[str]，并简单清洗。

    兼容旧 API: 仅识别 url/file_url/image_url/video_url 字段, 不分类。
    新的真实结构请用 _split_media。
    """
    if value is None:
        return []
    if isinstance(value, str):
        v = value.strip()
        return [v] if v else []
    if isinstance(value, (list, tuple)):
        result: List[str] = []
        for item in value:
            if isinstance(item, str):
                v = item.strip()
                if v:
                    result.append(v)
            elif isinstance(item, dict):
                url = (
                    item.get("url")
                    or item.get("file_url")
                    or item.get("image_url")
                    or item.get("video_url")
                )
                if isinstance(url, str) and url.strip():
                    result.append(url.strip())
        return result
    return []
