"""ConversationStore 单元测试 (薄 conversation_id 映射, 非会话历史)。"""

from __future__ import annotations

import pytest

from app.services.conversation_store import (
    InMemoryConversationStore,
    _key,
)


@pytest.mark.asyncio
async def test_get_returns_none_on_first_turn():
    store = InMemoryConversationStore()
    assert await store.get("user-1", "kf-xxx") is None


@pytest.mark.asyncio
async def test_save_then_get_roundtrip():
    store = InMemoryConversationStore()
    await store.save("user-1", "kf-xxx", "conv-abc")
    assert await store.get("user-1", "kf-xxx") == "conv-abc"


@pytest.mark.asyncio
async def test_scope_isolation_kf_vs_bot():
    store = InMemoryConversationStore()
    await store.save("user-1", "kf-xxx", "conv-kf")
    await store.save("user-1", "bot", "conv-bot")
    assert await store.get("user-1", "kf-xxx") == "conv-kf"
    assert await store.get("user-1", "bot") == "conv-bot"


@pytest.mark.asyncio
async def test_save_overwrites_on_new_conversation():
    store = InMemoryConversationStore()
    await store.save("user-1", "bot", "conv-old")
    await store.save("user-1", "bot", "conv-new")
    assert await store.get("user-1", "bot") == "conv-new"


@pytest.mark.asyncio
async def test_save_empty_id_is_noop():
    store = InMemoryConversationStore()
    await store.save("user-1", "bot", "")
    assert await store.get("user-1", "bot") is None


@pytest.mark.asyncio
async def test_historical_b_state_is_normalized_to_a_runtime_shape():
    store = InMemoryConversationStore()
    await store.save_state(
        "user-1",
        "bot",
        {
            "active": "B",
            "conv_a": "conv-a",
            "conv_b": "conv-b-old",
            "bug_v2_active": True,
        },
    )

    state = await store.get_state("user-1", "bot")
    assert state == {
        "active": "A",
        "conv_a": "conv-a",
        "conv_b": "",
        "bug_v2_active": True,
        "bug_v2_suspended": False,
    }


@pytest.mark.asyncio
async def test_suspended_bug_draft_state_survives_roundtrip():
    store = InMemoryConversationStore()
    await store.save_state(
        "user-1",
        "bot",
        {"bug_v2_active": False, "bug_v2_suspended": True},
    )
    assert (await store.get_state("user-1", "bot"))["bug_v2_suspended"] is True


def test_key_normalizes_empty_values():
    assert _key("", "") == ("anon", "default")
    assert _key("u", "s") == ("u", "s")
