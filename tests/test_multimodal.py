"""multimodal.py 单元测试。

覆盖:
    - _coerce_url_list (None / str / list[str] / list[dict] 各种形态)
    - extract_multimodal_payload (text / reply_content / content / data 各种字段)
    - compose_multimodal_markdown (文本+图片+视频+文件拼接)
    - has_multimodal_payload
"""
from __future__ import annotations

import pytest

from app.services.multimodal import (
    _coerce_url_list,
    compose_multimodal_markdown,
    extract_multimodal_payload,
    has_multimodal_payload,
)


# ---------------------------------------------------------------------------
# _coerce_url_list
# ---------------------------------------------------------------------------

class TestCoerceUrlList:
    def test_none_returns_empty(self):
        assert _coerce_url_list(None) == []

    def test_empty_string_returns_empty(self):
        assert _coerce_url_list("") == []

    def test_single_string(self):
        assert _coerce_url_list("https://x.com/a.jpg") == ["https://x.com/a.jpg"]

    def test_single_string_stripped(self):
        assert _coerce_url_list("  https://x.com/a.jpg  ") == ["https://x.com/a.jpg"]

    def test_list_of_strings(self):
        urls = ["https://x.com/a.jpg", "https://x.com/b.jpg"]
        assert _coerce_url_list(urls) == urls

    def test_list_of_strings_filters_empty(self):
        assert _coerce_url_list(["https://x.com/a.jpg", "", "  "]) == ["https://x.com/a.jpg"]

    def test_list_of_dicts_with_url_key(self):
        items = [{"url": "https://x.com/a.jpg"}]
        assert _coerce_url_list(items) == ["https://x.com/a.jpg"]

    def test_list_of_dicts_with_file_url_key(self):
        items = [{"file_url": "https://x.com/b.pdf"}]
        assert _coerce_url_list(items) == ["https://x.com/b.pdf"]

    def test_list_of_dicts_with_image_url_key(self):
        items = [{"image_url": "https://x.com/c.png"}]
        assert _coerce_url_list(items) == ["https://x.com/c.png"]

    def test_list_of_dicts_with_video_url_key(self):
        items = [{"video_url": "https://x.com/d.mp4"}]
        assert _coerce_url_list(items) == ["https://x.com/d.mp4"]

    def test_list_of_dicts_filters_empty_url(self):
        items = [{"url": ""}, {"url": "https://x.com/a.jpg"}]
        assert _coerce_url_list(items) == ["https://x.com/a.jpg"]

    def test_list_of_dicts_no_known_url_key_returns_empty(self):
        items = [{"other_key": "value"}]
        assert _coerce_url_list(items) == []

    def test_unsupported_type_returns_empty(self):
        assert _coerce_url_list(123) == []
        assert _coerce_url_list(True) == []


# ---------------------------------------------------------------------------
# extract_multimodal_payload
# ---------------------------------------------------------------------------

class TestExtractMultimodalPayload:
    def test_empty_dict(self):
        p = extract_multimodal_payload({})
        assert p == {"text": "", "images": [], "videos": [], "files": []}

    def test_none_returns_empty_payload(self):
        p = extract_multimodal_payload(None)  # type: ignore[arg-type]
        assert p == {"text": "", "images": [], "videos": [], "files": []}

    def test_top_level_text(self):
        p = extract_multimodal_payload({"text": "hello"})
        assert p["text"] == "hello"

    def test_reply_content_text_object(self):
        wf = {
            "reply_content": {
                "msgtype": "text",
                "text": {"content": "hello from reply_content"},
            }
        }
        assert extract_multimodal_payload(wf)["text"] == "hello from reply_content"

    def test_reply_content_fallback_content(self):
        wf = {"reply_content": {"content": "fallback content"}}
        assert extract_multimodal_payload(wf)["text"] == "fallback content"

    def test_top_level_content_field(self):
        assert extract_multimodal_payload({"content": "hi"})["text"] == "hi"

    def test_top_level_data_string(self):
        assert extract_multimodal_payload({"data": "raw data"})["text"] == "raw data"

    def test_text_priority_over_other_fields(self):
        wf = {
            "text": "primary",
            "content": "secondary",
            "data": "tertiary",
            "reply_content": {"text": {"content": "quaternary"}},
        }
        assert extract_multimodal_payload(wf)["text"] == "primary"

    def test_reply_content_priority_over_content(self):
        wf = {
            "reply_content": {"text": {"content": "primary rc"}},
            "content": "secondary content",
        }
        assert extract_multimodal_payload(wf)["text"] == "primary rc"

    def test_images_field(self):
        p = extract_multimodal_payload({"text": "x", "images": ["a.jpg", "b.jpg"]})
        assert p["images"] == ["a.jpg", "b.jpg"]

    def test_videos_field(self):
        p = extract_multimodal_payload({"videos": ["v.mp4"]})
        assert p["videos"] == ["v.mp4"]

    def test_files_field(self):
        p = extract_multimodal_payload({"files": ["d.pdf"]})
        assert p["files"] == ["d.pdf"]

    def test_missing_url_arrays_default_to_empty(self):
        p = extract_multimodal_payload({"text": "x"})
        assert p["images"] == []
        assert p["videos"] == []
        assert p["files"] == []


# ---------------------------------------------------------------------------
# compose_multimodal_markdown
# ---------------------------------------------------------------------------

class TestComposeMultimodalMarkdown:
    def test_text_only(self):
        md = compose_multimodal_markdown({"text": "hello"})
        assert md == "hello"

    def test_text_stripped(self):
        md = compose_multimodal_markdown({"text": "  hello  "})
        assert md == "hello"

    def test_single_image(self):
        md = compose_multimodal_markdown({"images": ["https://x.com/a.jpg"]})
        assert md == "\n\n![图片](https://x.com/a.jpg)"

    def test_multiple_images(self):
        md = compose_multimodal_markdown({
            "images": ["https://x.com/a.jpg", "https://x.com/b.jpg"]
        })
        assert "![图片](https://x.com/a.jpg)" in md
        assert "![图片](https://x.com/b.jpg)" in md
        # 两张图各自独立一段
        assert md.count("![图片]") == 2

    def test_custom_image_alt(self):
        md = compose_multimodal_markdown(
            {"images": ["https://x.com/a.jpg"]},
            image_alt="产品图",
        )
        assert "![产品图](https://x.com/a.jpg)" in md

    def test_video_renders_as_link(self):
        md = compose_multimodal_markdown({"videos": ["https://x.com/v.mp4"]})
        assert "[视频](https://x.com/v.mp4)" in md
        assert "!" not in md  # 视频不是 markdown 图片

    def test_file_renders_as_link(self):
        md = compose_multimodal_markdown({"files": ["https://x.com/d.pdf"]})
        assert "[文件](https://x.com/d.pdf)" in md

    def test_text_plus_images(self):
        md = compose_multimodal_markdown({
            "text": "这是产品图",
            "images": ["https://x.com/a.jpg"],
        })
        assert md.startswith("这是产品图")
        assert "![图片](https://x.com/a.jpg)" in md

    def test_full_payload(self):
        md = compose_multimodal_markdown({
            "text": "回答文本",
            "images": ["https://x.com/a.jpg", "https://x.com/b.jpg"],
            "videos": ["https://x.com/v.mp4"],
            "files": ["https://x.com/d.pdf"],
        })
        assert md.startswith("回答文本")
        assert "![图片](https://x.com/a.jpg)" in md
        assert "![图片](https://x.com/b.jpg)" in md
        assert "[视频](https://x.com/v.mp4)" in md
        assert "[文件](https://x.com/d.pdf)" in md

    def test_coze_reply_content_format(self):
        """Coze stream_run 返回的 reply_content 结构应正确拼接"""
        wf = {
            "reply_content": {"msgtype": "text", "text": {"content": "hi from coze"}},
            "images": ["https://x.com/a.jpg"],
        }
        md = compose_multimodal_markdown(wf)
        assert "hi from coze" in md
        assert "![图片](https://x.com/a.jpg)" in md

    def test_empty_returns_empty_string(self):
        assert compose_multimodal_markdown({}) == ""

    def test_image_only_no_text(self):
        md = compose_multimodal_markdown({"images": ["https://x.com/a.jpg"]})
        # 无文本时只有图片，不应出现 "None" / "null"
        assert "None" not in md
        assert "null" not in md
        assert "![图片](https://x.com/a.jpg)" in md

    def test_images_with_dict_form(self):
        md = compose_multimodal_markdown({
            "images": [{"url": "https://x.com/a.jpg"}]
        })
        assert "![图片](https://x.com/a.jpg)" in md


# ---------------------------------------------------------------------------
# has_multimodal_payload
# ---------------------------------------------------------------------------

class TestHasMultimodalPayload:
    def test_text_only_no_multimodal(self):
        assert has_multimodal_payload({"text": "hello"}) is False

    def test_empty_no_multimodal(self):
        assert has_multimodal_payload({}) is False

    def test_images_present(self):
        assert has_multimodal_payload({"images": ["x.jpg"]}) is True

    def test_videos_present(self):
        assert has_multimodal_payload({"videos": ["v.mp4"]}) is True

    def test_files_present(self):
        assert has_multimodal_payload({"files": ["d.pdf"]}) is True

    def test_empty_arrays_not_multimodal(self):
        assert has_multimodal_payload({"images": [], "videos": [], "files": []}) is False