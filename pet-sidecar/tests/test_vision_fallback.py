"""Vision fallback: describe screenshots for text-only chat models.

The default chat provider (deepseek-web browser bridge) cannot consume
images. ``capture_screen`` must still work for "looking" by routing the
screenshot through an OpenAI-compatible vision provider and returning only
the description to the chat model. No real API calls here.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.providers import ProviderRegistry
from gateway.providers.anthropic import AnthropicProvider
from gateway.providers.openai import OpenAIProvider
from gateway.server import (
    _SCREEN_DESCRIBE_SYSTEM,
    _describe_screen_with_provider,
    _select_vision_provider,
)


class _FakeVisionProvider:
    """OpenAIProvider-shaped fake: records kwargs and yields scripted chunks."""

    def __init__(self, chunks=None, *, exc=None, delay=0.0) -> None:
        self.name = "deepseek"
        self.chunks = list(chunks or [])
        self.exc = exc
        self.delay = delay
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if self.exc is not None:
            raise self.exc
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk


def _run(coro):
    return asyncio.run(coro)


# ── _describe_screen_with_provider ─────────────────────────────────────


def test_describe_joins_deltas_ignores_think_and_sends_image():
    provider = _FakeVisionProvider([
        {"type": "think", "content": "internal reasoning"},
        {"type": "delta", "content": "  屏幕上是 "},
        {"type": "delta", "content": "VS Code 编辑器。"},
        {"type": "end"},
    ])

    description = _run(_describe_screen_with_provider(provider, "QUJD"))

    assert description == "屏幕上是 VS Code 编辑器。"
    call = provider.calls[0]
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "截图" in content[0]["text"]
    image = content[1]["image_url"]["url"]
    assert image.startswith("data:image/jpeg;base64,")
    assert image.endswith("QUJD")
    assert "视觉描述器" in call["system"]
    assert call["system"] == _SCREEN_DESCRIBE_SYSTEM
    assert call["tools"] is None
    assert call["max_tokens"] == 900
    assert call["temperature"] == 0.2
    assert call["top_p"] == 0.9
    assert call["thinking_override"] is False
    assert call["reasoning_effort_override"] is None


def test_describe_raises_on_provider_error_event():
    provider = _FakeVisionProvider([
        {"type": "delta", "content": "部分"},
        {"type": "error", "message": "API key invalid"},
    ])

    with pytest.raises(RuntimeError, match="API key invalid"):
        _run(_describe_screen_with_provider(provider, "QUJD"))


def test_describe_raises_on_exception():
    provider = _FakeVisionProvider(exc=ConnectionError("bridge down"))

    with pytest.raises(RuntimeError, match="bridge down"):
        _run(_describe_screen_with_provider(provider, "QUJD"))


def test_describe_raises_on_empty_answer():
    provider = _FakeVisionProvider([
        {"type": "think", "content": "only thinking"},
        {"type": "end"},
    ])

    with pytest.raises(RuntimeError, match="没有返回内容"):
        _run(_describe_screen_with_provider(provider, "QUJD"))


def test_describe_raises_on_timeout():
    provider = _FakeVisionProvider(
        [{"type": "delta", "content": "slow"}], delay=5.0,
    )

    with pytest.raises(RuntimeError, match="超时"):
        _run(_describe_screen_with_provider(provider, "QUJD", timeout=0.01))


# ── _select_vision_provider ────────────────────────────────────────────


def _vision(model: str = "deepseek-v4-flash", name: str = "deepseek"):
    return OpenAIProvider(model=model, name=name)


def test_text_only_active_selects_deepseek_candidate():
    active = OpenAIProvider(
        model="browser-bridge", name="deepseek-web", supports_vision=False,
    )
    candidate = _vision()
    registry = ProviderRegistry()
    registry.register(active)
    registry.register(candidate)

    chosen = _select_vision_provider(active, registry, env={})

    assert chosen is candidate


def test_vision_capable_active_is_returned_as_is():
    active = _vision(name="active-vision")
    registry = ProviderRegistry()
    registry.register(_vision())

    assert _select_vision_provider(active, registry, env={}) is active


def test_text_only_active_is_never_selected_again():
    active = OpenAIProvider(
        model="browser-bridge", name="deepseek", supports_vision=False,
    )
    registry = ProviderRegistry()
    registry.register(active)

    assert _select_vision_provider(active, registry, env={}) is None


def test_env_name_takes_priority_over_defaults():
    env_provider = _vision(name="硅基流动")
    registry = ProviderRegistry()
    registry.register(_vision())  # deepseek
    registry.register(env_provider)

    chosen = _select_vision_provider(None, registry, env={"PET_VISION_PROVIDER": "硅基流动"})

    assert chosen is env_provider


def test_siliconflow_is_a_fallback_name():
    provider = _vision(name="siliconflow")
    registry = ProviderRegistry()
    registry.register(provider)

    assert _select_vision_provider(None, registry, env={"PET_VISION_PROVIDER": "missing"}) is provider


def test_non_openai_candidate_is_skipped():
    registry = ProviderRegistry()
    registry.register(AnthropicProvider(api_key="k", model="claude-sonnet-4", name="deepseek"))

    assert _select_vision_provider(None, registry, env={}) is None


def test_no_candidate_returns_none():
    assert _select_vision_provider(None, ProviderRegistry(), env={}) is None
