"""真实工作流结构的多模态解析测试 (用户实际产出形态)。

覆盖:
    - 顶层 assistant_text
    - 顶层 media 数组按 type 分类
    - 嵌套 output 字段 (<think>...{json}...</think>)
    - text 与多模态合并输出
    - compose_multimodal_markdown 完整流程
"""
from __future__ import annotations

import json

from app.services.multimodal import (
    compose_multimodal_markdown,
    extract_multimodal_payload,
    _strip_thinking,
    _extract_text_from_nested_json,
)


def _real_workflow_payload():
    """复刻用户提供的真实工作流输出结构 (Coze stream_run SSE message content 解析后形态)。"""
    inner_output_str = json.dumps({
        "text": "您好，充电桩PC管理后端的角色管理操作步骤如下：\n1. ...",
        "media": [
            {
                "type": "image",
                "url": "https://resource.charging.com/files/701d0ec7/file-preview?sign=xxx1",
                "description": "角色管理页面点击添加按钮操作示意",
            },
            {
                "type": "image",
                "url": "https://resource.charging.com/files/bf244b8b/file-preview?sign=xxx2",
                "description": "角色信息表单填写页面示意",
            },
            {
                "type": "image",
                "url": "https://resource.charging.com/files/c7f23312/file-preview?sign=xxx3",
                "description": "角色权限配置入口页面示意",
            },
            {
                "type": "image",
                "url": "https://resource.charging.com/files/fef7c256/file-preview?sign=xxx4",
                "description": "角色权限勾选更新页面示意",
            },
        ],
    }, ensure_ascii=False)

    return {
        "assistant_text": "您好，充电桩PC管理后端的角色管理操作步骤如下：\n1. 进入系统后，点击菜单中的「角色管理」选项...",
        "image_id": None,
        "audio_id": None,
        "media": [],  # 顶层 media 是空数组, 真实数据在嵌套 output 里
        "raw": {
            "task_id": "59c50d5b",
            "workflow_run_id": "661322b3",
            "data": {
                "id": "661322b3",
                "workflow_id": "d7066469",
                "status": "succeeded",
                "outputs": {
                    "output": f"<think>\n关于充电桩PC管理后端角色管理的操作，我将梳理步骤并配图说明。\n</think>{inner_output_str}",
                },
                "elapsed_time": 35.6,
                "total_tokens": 3711,
            },
        },
    }


class TestStripThinking:
    def test_strips_single_think_block(self):
        result = _strip_thinking("<think>\nthinking...\n</think>实际答案")
        assert result == "实际答案"

    def test_strips_multiple_think_blocks(self):
        result = _strip_thinking(
            "<think>第一段思考</think>\n中间文本\n<think>第二段思考</think>\n最终答案"
        )
        assert "中间文本" in result
        assert "最终答案" in result
        assert "思考" not in result

    def test_no_think_block(self):
        result = _strip_thinking("普通文本")
        assert result == "普通文本"

    def test_empty_string(self):
        assert _strip_thinking("") == ""
        assert _strip_thinking(None) == ""  # type: ignore[arg-type]

    def test_only_think_block(self):
        result = _strip_thinking("<think>只有思考</think>")
        assert result == ""


class TestExtractTextFromNestedJson:
    def test_parses_nested_json_with_text_field(self):
        nested = '{"text": "实际答案", "media": []}'
        result = _extract_text_from_nested_json(f"<think>思考</think>{nested}")
        assert result == "实际答案"

    def test_strips_thinking_before_parse(self):
        result = _extract_text_from_nested_json(
            "<think>一些思考过程</think>{\"text\": \"答案\"}"
        )
        assert result == "答案"

    def test_invalid_json_falls_back_to_string(self):
        result = _extract_text_from_nested_json("<think>思考</think>普通文本不是 JSON")
        assert result == "普通文本不是 JSON"

    def test_empty_after_strip(self):
        result = _extract_text_from_nested_json("<think>只有思考</think>")
        assert result == ""


class TestRealWorkflowStructure:
    def test_top_level_assistant_text_takes_priority(self):
        wf = _real_workflow_payload()
        payload = extract_multimodal_payload(wf)
        assert payload["text"].startswith("您好，充电桩PC管理后端")
        # 顶层 assistant_text 优先于嵌套 output.text
        assert "1. 进入系统后" in payload["text"]

    def test_media_extracted_from_nested_output(self):
        """真实结构: 顶层 media 是空, 图片在嵌套 output.media 里"""
        wf = _real_workflow_payload()
        payload = extract_multimodal_payload(wf)
        assert len(payload["images"]) == 4
        for url in payload["images"]:
            assert url.startswith("https://resource.charging.com/files/")
        # videos/files 应该是空 (真实工作流只产出 image)
        assert payload["videos"] == []
        assert payload["files"] == []

    def test_no_think_block_in_text(self):
        """最终 text 不能包含 <think> 思考块"""
        wf = _real_workflow_payload()
        payload = extract_multimodal_payload(wf)
        assert "<think>" not in payload["text"]
        assert "</think>" not in payload["text"]
        assert "我将梳理步骤" not in payload["text"]  # 思考过程被过滤

    def test_compose_real_workflow_to_markdown(self):
        """完整流程: 真实结构 → markdown 文本"""
        wf = _real_workflow_payload()
        md = compose_multimodal_markdown(wf)

        # 文本在前
        assert md.startswith("您好，充电桩PC管理后端")
        # 4 张图片内嵌
        assert md.count("![图片]") == 4
        assert "https://resource.charging.com/files/701d0ec7/file-preview" in md
        assert "https://resource.charging.com/files/fef7c256/file-preview" in md
        # 没有 think 块泄漏
        assert "<think>" not in md


class TestMediaTypeSplit:
    """media 数组里按 type 字段分桶"""

    def test_image_type_to_images(self):
        wf = {
            "media": [
                {"type": "image", "url": "https://x/a.jpg"},
                {"type": "image", "url": "https://x/b.jpg"},
            ]
        }
        p = extract_multimodal_payload(wf)
        assert len(p["images"]) == 2
        assert p["videos"] == []
        assert p["files"] == []

    def test_video_type_to_videos(self):
        wf = {
            "media": [
                {"type": "video", "url": "https://x/v.mp4"},
            ]
        }
        p = extract_multimodal_payload(wf)
        assert p["videos"] == ["https://x/v.mp4"]
        assert p["images"] == []

    def test_file_type_to_files(self):
        wf = {
            "media": [
                {"type": "file", "url": "https://x/d.pdf"},
                {"type": "document", "url": "https://x/d.docx"},
            ]
        }
        p = extract_multimodal_payload(wf)
        assert p["files"] == ["https://x/d.pdf", "https://x/d.docx"]

    def test_mixed_media_types(self):
        wf = {
            "media": [
                {"type": "image", "url": "https://x/a.jpg"},
                {"type": "video", "url": "https://x/v.mp4"},
                {"type": "file", "url": "https://x/d.pdf"},
                {"type": "image", "url": "https://x/b.jpg"},
            ]
        }
        p = extract_multimodal_payload(wf)
        assert p["images"] == ["https://x/a.jpg", "https://x/b.jpg"]
        assert p["videos"] == ["https://x/v.mp4"]
        assert p["files"] == ["https://x/d.pdf"]

    def test_no_type_defaults_to_image(self):
        wf = {
            "media": [
                {"url": "https://x/a.jpg"},  # 无 type 字段
            ]
        }
        p = extract_multimodal_payload(wf)
        assert p["images"] == ["https://x/a.jpg"]


class TestBackwardCompatSimpleFormat:
    """旧的简单结构 {output, images, videos, files} 仍能工作"""

    def test_simple_output_and_images(self):
        wf = {
            "output": "简单文本",
            "images": ["https://x/a.jpg"],
        }
        p = extract_multimodal_payload(wf)
        assert p["text"] == "简单文本"
        assert p["images"] == ["https://x/a.jpg"]

    def test_simple_top_level_text(self):
        wf = {"text": "纯文本"}
        p = extract_multimodal_payload(wf)
        assert p["text"] == "纯文本"
        assert p["images"] == []

    def test_simple_text_plus_videos_files(self):
        wf = {
            "text": "回答",
            "videos": ["https://x/v.mp4"],
            "files": ["https://x/d.pdf"],
        }
        p = extract_multimodal_payload(wf)
        assert p["videos"] == ["https://x/v.mp4"]
        assert p["files"] == ["https://x/d.pdf"]