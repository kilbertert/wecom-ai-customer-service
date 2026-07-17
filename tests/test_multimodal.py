"""multimodal.py 单元测试。

覆盖:
    - _coerce_url_list (None / str / list[str] / list[dict] 各种形态)
    - extract_multimodal_payload (text / content / data 各种字段)
    - compose_multimodal_markdown (文本+图片+视频+文件拼接)
    - has_multimodal_payload
"""

from __future__ import annotations

import pytest

from app.services.multimodal import (_coerce_url_list,
                                     compose_multimodal_markdown,
                                     extract_multimodal_payload,
                                     has_multimodal_payload)

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
        assert _coerce_url_list(["https://x.com/a.jpg", "", "  "]) == [
            "https://x.com/a.jpg"
        ]

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

    def test_top_level_content_field(self):
        assert extract_multimodal_payload({"content": "hi"})["text"] == "hi"

    def test_top_level_data_string(self):
        assert extract_multimodal_payload({"data": "raw data"})["text"] == "raw data"

    def test_text_priority_over_other_fields(self):
        wf = {
            "text": "primary",
            "content": "secondary",
            "data": "tertiary",
        }
        assert extract_multimodal_payload(wf)["text"] == "primary"

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
        md = compose_multimodal_markdown(
            {"images": ["https://x.com/a.jpg", "https://x.com/b.jpg"]}
        )
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
        md = compose_multimodal_markdown(
            {
                "text": "这是产品图",
                "images": ["https://x.com/a.jpg"],
            }
        )
        assert md.startswith("这是产品图")
        assert "![图片](https://x.com/a.jpg)" in md

    def test_full_payload(self):
        md = compose_multimodal_markdown(
            {
                "text": "回答文本",
                "images": ["https://x.com/a.jpg", "https://x.com/b.jpg"],
                "videos": ["https://x.com/v.mp4"],
                "files": ["https://x.com/d.pdf"],
            }
        )
        assert md.startswith("回答文本")
        assert "![图片](https://x.com/a.jpg)" in md
        assert "![图片](https://x.com/b.jpg)" in md
        assert "[视频](https://x.com/v.mp4)" in md
        assert "[文件](https://x.com/d.pdf)" in md

    def test_empty_returns_empty_string(self):
        assert compose_multimodal_markdown({}) == ""


# ---------------------------------------------------------------------------
# 文本中裸的图片 URL 自动包装为 ![图片](url) 语法
# (Dify workflow 常把图片 URL 嵌在 text 字符串里, 需要扫描转 markdown 图片)
# ---------------------------------------------------------------------------


class TestConvertInlineImageUrls:
    """compose_multimodal_markdown 应识别 text 字符串里的裸图片 URL 并包装。"""

    def test_png_url_in_text_wraps_to_markdown_image(self):
        """Dify 实际场景: text 末尾含 .png URL, 期望自动包装"""
        text = "已记录您反馈的充电桩故障问题，操作指引参考图示：https://trendpower-ai-customer-service.oss-cn-guangzhou.aliyuncs.com/kb/charge-pile/pc-backend-equipment-failure-list-1.png。若您是启动充电失败"
        md = compose_multimodal_markdown({"text": text})
        assert (
            "![图片](https://trendpower-ai-customer-service.oss-cn-guangzhou.aliyuncs.com/kb/charge-pile/pc-backend-equipment-failure-list-1.png)"
            in md
        )
        # 原来的裸 URL 不应残留 (否则 WeChat 会同时显示链接和图片)
        assert "参考图示：https://" not in md

    def test_jpg_url_wraps(self):
        text = "看图 https://x.com/a.jpg 完"
        md = compose_multimodal_markdown({"text": text})
        assert "![图片](https://x.com/a.jpg)" in md

    def test_jpeg_gif_webp_svg_bmp_wraps(self):
        for ext in ["jpeg", "gif", "webp", "svg", "bmp", "JPG", "PNG"]:
            url = f"https://x.com/a.{ext}"
            text = f"看 {url} 完"
            md = compose_multimodal_markdown({"text": text})
            assert f"![图片]({url})" in md, f"Failed for ext={ext}, md={md!r}"

    def test_non_image_url_not_wrapped(self):
        """非图片扩展名 (.pdf/.html/.mp4 等) 不应被包装为图片"""
        text = "文档 https://x.com/a.pdf 链接"
        md = compose_multimodal_markdown({"text": text})
        assert "https://x.com/a.pdf" in md
        assert "![图片]" not in md

    def test_already_marked_down_image_not_double_wrapped(self):
        """text 里已经有 ![alt](url) 语法的, 不应再包一层"""
        text = "看图 ![产品图](https://x.com/a.png) 完"
        md = compose_multimodal_markdown({"text": text})
        # 应保持原样
        assert "![产品图](https://x.com/a.png)" in md
        # 不应变成嵌套
        assert "![图片](![产品图]" not in md
        # 只应有 1 个 ![ 而非 2 个
        assert md.count("![") == 1

    def test_url_with_query_string_wraps(self):
        """URL 带 query string 也要识别"""
        text = "看图 https://x.com/a.png?v=123&t=abc 完"
        md = compose_multimodal_markdown({"text": text})
        assert "![图片](https://x.com/a.png?v=123&t=abc)" in md

    def test_url_at_end_of_text_wraps(self):
        """text 末尾是 URL, 无后续标点时也要包"""
        text = "看图 https://x.com/a.png"
        md = compose_multimodal_markdown({"text": text})
        assert "![图片](https://x.com/a.png)" in md

    def test_custom_image_alt_applies(self):
        """自定义 image_alt 也应生效"""
        text = "看图 https://x.com/a.png"
        md = compose_multimodal_markdown({"text": text}, image_alt="示意图")
        assert "![示意图](https://x.com/a.png)" in md

    def test_multiple_image_urls_in_one_text(self):
        """text 里多张图都应包装"""
        text = "看 https://x.com/a.png 然后 https://x.com/b.jpg"
        md = compose_multimodal_markdown({"text": text})
        assert "![图片](https://x.com/a.png)" in md
        assert "![图片](https://x.com/b.jpg)" in md
        assert md.count("![图片]") == 2

    def test_image_url_then_separate_images_array_dedup(self):
        """text 里有的 + images 数组里也有的 URL, 不应重复"""
        url = "https://x.com/a.png"
        md = compose_multimodal_markdown(
            {
                "text": f"看图 {url}",
                "images": [url],
            }
        )
        # URL 只出现一次 (text 中已包成 ![图片](url), 不再附加末尾)
        assert md.count(url) == 1
        # 只 1 个 ![图片]
        assert md.count("![图片]") == 1

    def test_image_only_no_text(self):
        md = compose_multimodal_markdown({"images": ["https://x.com/a.jpg"]})
        # 无文本时只有图片，不应出现 "None" / "null"
        assert "None" not in md
        assert "null" not in md
        assert "![图片](https://x.com/a.jpg)" in md

    def test_images_with_dict_form(self):
        md = compose_multimodal_markdown({"images": [{"url": "https://x.com/a.jpg"}]})
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
        assert (
            has_multimodal_payload({"images": [], "videos": [], "files": []}) is False
        )
