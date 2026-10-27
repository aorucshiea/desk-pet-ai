"""Vision capability must follow the ACTIVE chat provider.

Live bug: the user's provider is DeepSeek (a cloud vision-capable model),
but ``capture_screen`` only looked at the local llama-server's mmproj and
told the model "you have no vision — use observe_screen". The screenshot
tool and the tool-result image attachment path must share one capability
check, and providers.json must be able to override it explicitly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from gateway import server as server_mod
from gateway.providers import (
    ProviderRegistry,
    create_providers,
    provider_can_see_images,
    vision_capability_from_config,
)
from gateway.providers.anthropic import AnthropicProvider
from gateway.providers.local import LocalProvider
from gateway.providers.openai import OpenAIProvider
from gateway.server import ChatRequest


class _Server:
    """Minimal LlamaServer stand-in: only mmproj_path matters here."""

    def __init__(self, mmproj: str | Path | None = None) -> None:
        self.mmproj_path = Path(mmproj) if mmproj else None
        self.model_path = None


# ── OpenAI-compatible heuristic ────────────────────────────────────────


def test_deepseek_model_is_assumed_vision_capable():
    provider = OpenAIProvider(
        model="deepseek-v4-flash",
        name="deepseek",
        thinking=True,
    )

    assert provider.supports_vision is True
    assert provider_can_see_images(provider) is True


def test_known_text_only_models_are_rejected():
    for model in ("gpt-3.5-turbo", "text-embedding-3-large", "text-davinci-003"):
        provider = OpenAIProvider(model=model)
        assert provider.supports_vision is False, model
        assert provider_can_see_images(provider) is False, model


def test_custom_provider_explicit_capability_wins():
    provider = OpenAIProvider(model="gpt-3.5-turbo", supports_vision=True)
    assert provider_can_see_images(provider) is True

    provider = OpenAIProvider(model="deepseek-v4-flash", supports_vision=False)
    assert provider_can_see_images(provider) is False


def test_anthropic_always_has_vision():
    provider = AnthropicProvider(api_key="k", model="claude-sonnet-4")
    assert provider_can_see_images(provider, _Server()) is True


# ── Local provider / missing provider fallback ─────────────────────────


def test_local_provider_vision_requires_mmproj():
    assert provider_can_see_images(LocalProvider(_Server("mmproj.gguf"))) is True
    assert provider_can_see_images(LocalProvider(_Server())) is False


def test_no_active_provider_falls_back_to_local_mmproj():
    assert provider_can_see_images(None, _Server("mmproj.gguf")) is True
    assert provider_can_see_images(None, _Server()) is False
    assert provider_can_see_images(None, None) is False


# ── providers.json override ─────────────────────────────────────────────


def test_vision_override_reads_both_config_shapes():
    assert vision_capability_from_config({"capabilities": {"images": False}}) is False
    assert vision_capability_from_config({"capabilities": {"images": "true"}}) is True
    assert vision_capability_from_config({"images": True}) is True
    assert vision_capability_from_config({"capabilities": {}}) is None
    assert vision_capability_from_config({}) is None
    assert vision_capability_from_config(None) is None


# ── LM Studio-style models[]: selected model capability wins ────────────

_LMSTUDIO_CFG = {
    "provider": "lmstudio",
    "apiKey": "lm-studio",
    "baseUrl": "http://127.0.0.1:1234/v1",
    "model": "minicpm5-2b",
    "models": [
        {"id": "local-model", "capabilities": {"tools": True, "images": False}},
        {"id": "minicpm5-2b", "capabilities": {"tools": True, "images": False}},
        {"id": "neohorse-1-9b", "capabilities": {"tools": True, "images": True}},
    ],
}


def test_lmstudio_selected_model_images_false_beats_heuristic():
    """minicpm5-2b has no name marker, so the heuristic alone says True.
    providers.json explicitly marks it text-only — that must win."""
    assert vision_capability_from_config(_LMSTUDIO_CFG) is False
    registry = create_providers(provider_configs=[_LMSTUDIO_CFG])
    provider = registry.get("lmstudio")
    assert provider.supports_vision is False
    assert provider_can_see_images(provider) is False


def test_lmstudio_selected_model_images_true():
    cfg = dict(_LMSTUDIO_CFG, model="neohorse-1-9b")
    assert vision_capability_from_config(cfg) is True
    provider = create_providers(provider_configs=[cfg]).get("lmstudio")
    assert provider.supports_vision is True


def test_lmstudio_unknown_model_keeps_heuristic():
    cfg = dict(_LMSTUDIO_CFG, model="some-new-4b")
    assert vision_capability_from_config(cfg) is None
    provider = create_providers(provider_configs=[cfg]).get("lmstudio")
    assert provider.supports_vision is True


def test_top_level_override_beats_selected_model_capability():
    flat = dict(_LMSTUDIO_CFG, images=True)
    assert vision_capability_from_config(flat) is True
    nested = dict(_LMSTUDIO_CFG, capabilities={"images": True})
    assert vision_capability_from_config(nested) is True


def test_create_providers_passes_capabilities_images_through():
    registry = create_providers(provider_configs=[{
        "provider": "deepseek",
        "apiKey": "test-key",
        "baseUrl": "https://api.deepseek.com/v1",
        "model": "gpt-3.5-turbo",  # heuristic would say no
        "capabilities": {"images": True},
    }])
    provider = registry.get("deepseek")
    assert provider.supports_vision is True
    assert provider_can_see_images(provider) is True


def test_create_providers_flat_images_flag_overrides_heuristic():
    registry = create_providers(provider_configs=[{
        "provider": "deepseek",
        "apiKey": "test-key",
        "baseUrl": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
        "images": False,
    }])
    provider = registry.get("deepseek")
    assert provider.supports_vision is False
    assert provider_can_see_images(provider) is False


def test_create_providers_without_override_keeps_heuristic():
    registry = create_providers(provider_configs=[{
        "provider": "deepseek",
        "apiKey": "test-key",
        "baseUrl": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
    }])
    assert registry.get("deepseek").supports_vision is True


def test_registry_list_exposes_supports_vision():
    registry = ProviderRegistry()
    registry.register(OpenAIProvider(
        model="deepseek-v4-flash", name="deepseek",
    ))
    entry = {e["name"]: e for e in registry.list()}["deepseek"]
    assert entry["supports_vision"] is True


# ── Screen tool: active provider first, mmproj fallback second ─────────


def test_screen_tool_uses_active_cloud_provider_even_without_mmproj():
    provider = OpenAIProvider(model="deepseek-v4-flash", name="deepseek")
    token = server_mod._active_chat_provider_ref.set(provider)
    try:
        assert server_mod._screen_tool_can_see_images(_Server()) is True
    finally:
        server_mod._active_chat_provider_ref.reset(token)


def test_screen_tool_respects_active_text_only_model_even_with_mmproj():
    """The selected LM Studio model wins over the server's projector flag."""
    provider = OpenAIProvider(
        model="minicpm5-2b", name="lmstudio", supports_vision=False,
    )
    token = server_mod._active_chat_provider_ref.set(provider)
    try:
        assert server_mod._screen_tool_can_see_images(_Server("mmproj.gguf")) is False
    finally:
        server_mod._active_chat_provider_ref.reset(token)


def test_screen_tool_without_active_provider_uses_mmproj():
    assert server_mod._screen_tool_can_see_images(_Server("mmproj.gguf")) is True
    assert server_mod._screen_tool_can_see_images(_Server()) is False


# ── Wiring: _stream_chat_provider publishes its resolved provider ───────


class _ProbeProvider:
    name = "deepseek"
    supports_function_calling = True
    allow_tools = True

    def __init__(self) -> None:
        self.seen = "not-called"

    async def chat(self, **kwargs):
        self.seen = server_mod._active_chat_provider()
        yield {"type": "delta", "content": "看到了。"}
        yield {"type": "end"}


class _StubBridge:
    def new_session(self) -> None: ...
    def post(self, *_a, **_k) -> None: ...


class _StubMood:
    emotion_index = 0.0

    def format_for_system_prompt(self) -> str:
        return ""

    def apply_emotion_tag(self, _tag) -> None: ...


class _StubRegistry:
    def __init__(self, provider) -> None:
        self._providers = {"deepseek": provider}

    def get(self, name):
        return self._providers[name]


def test_stream_chat_publishes_active_provider_for_tool_handlers(monkeypatch):
    provider = _ProbeProvider()
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *a, **k: provider)
    monkeypatch.setattr(server_mod, "_kernel", lambda: None, raising=False)
    monkeypatch.setattr(server_mod, "get_event_store", lambda: None, raising=False)
    monkeypatch.setattr(server_mod, "_extraction_hook", None)
    monkeypatch.setattr(server_mod, "_ephemeral_ref", None)
    monkeypatch.setattr(server_mod, "_body_log_ref", None)
    monkeypatch.setattr(server_mod, "_decision_loop_ref", None)

    req = ChatRequest(
        messages=[{"role": "user", "content": "看看我的屏幕"}],
        tools_enabled=False,
    )

    async def _drain() -> object:
        async for _chunk in server_mod._stream_chat_provider(
            _StubRegistry(provider),
            mcp_manager=None,
            bridge=_StubBridge(),
            req=req,
            server=_Server(),
            server_state={},
            mood_store=_StubMood(),
        ):
            pass
        # Same task/context as the generator: a leaked set() would be
        # visible here (asyncio.run would otherwise hide it behind a
        # copied context).
        return server_mod._active_chat_provider()

    leftover = asyncio.run(_drain())

    assert provider.seen is provider, (
        "capture_screen runs inside the chat task and must see the "
        "provider that is actually serving this turn"
    )
    assert leftover is None, (
        "the turn must reset its provider ContextVar on completion"
    )
