"""Provider registry and factory.

Usage:
    registry = ProviderRegistry()
    registry.register(LocalProvider(server))
    registry.register(OpenAIProvider(api_key="..."))

    provider = registry.get("openai")  # raises KeyError if missing
    provider = registry.get_or("local")  # returns LocalProvider as fallback
"""

from __future__ import annotations

import os
from typing import Optional

from ..llama_client import LlamaServer
from .base import BaseProvider
from .local import LocalProvider
from .openai import OpenAIProvider, is_local_url
from .anthropic import AnthropicProvider


def _config_bool(value) -> Optional[bool]:
    """Coerce a providers.json capability value to bool without surprises."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
    return None


def _selected_model_images(cfg: dict) -> Optional[bool]:
    """Read ``capabilities.images`` of the model selected by ``cfg["model"]``.

    providers.json may carry a per-provider ``models`` array (LM Studio /
    OpenRouter style): the top-level ``model`` is the one actually used, so
    only that entry's capability applies. Unknown/missing model → None.
    """
    models = cfg.get("models")
    if not isinstance(models, list):
        return None
    selected = str(cfg.get("model") or "").strip()
    if not selected:
        return None
    for entry in models:
        if not isinstance(entry, dict):
            continue
        ids = [entry.get("id"), entry.get("model"), entry.get("name")]
        if not any(
            isinstance(candidate, str) and candidate.strip() == selected
            for candidate in ids
        ):
            continue
        caps = entry.get("capabilities")
        if isinstance(caps, dict) and "images" in caps:
            return _config_bool(caps.get("images"))
        if "images" in entry:
            return _config_bool(entry.get("images"))
        return None
    return None


def vision_capability_from_config(cfg: dict) -> Optional[bool]:
    """Read the optional explicit vision override from a provider config.

    Accepts either shape::

        {"capabilities": {"images": false}}
        {"images": false}

    Providers with a ``models`` array (LM Studio) are also matched against
    the selected top-level ``model`` and that entry's capability is used.
    Priority: top-level explicit override > selected model capabilities >
    the caller's model-name heuristic (None).
    """
    if not isinstance(cfg, dict):
        return None
    caps = cfg.get("capabilities")
    if isinstance(caps, dict) and "images" in caps:
        explicit = _config_bool(caps.get("images"))
        if explicit is not None:
            return explicit
    if "images" in cfg:
        explicit = _config_bool(cfg.get("images"))
        if explicit is not None:
            return explicit
    model_images = _selected_model_images(cfg)
    if model_images is not None:
        return model_images
    return None


def _server_has_vision(server) -> bool:
    try:
        return bool(getattr(server, "mmproj_path", None))
    except Exception:
        return False


def provider_can_see_images(provider, server=None) -> bool:
    """Whether *provider* can consume image inputs in the current gateway.

    This is the single source of truth shared by the screenshot tool and the
    tool-result image attachment path:

    - Anthropic: all current Claude models.
    - OpenAI-compatible: explicit ``supports_vision`` (providers.json) wins,
      otherwise the model-name heuristic. DeepSeek and other unlisted cloud
      models are assumed capable.
    - LocalProvider / no active provider: the local llama-server can only
      see images when a ``--mmproj`` projector is loaded.
    """
    if provider is None:
        return _server_has_vision(server)
    if isinstance(provider, AnthropicProvider):
        return True
    if isinstance(provider, OpenAIProvider):
        return bool(provider.supports_vision)
    if isinstance(provider, LocalProvider):
        own_server = getattr(provider, "_server", None)
        return _server_has_vision(server) or _server_has_vision(own_server)
    # Unknown/custom provider: prefer an explicitly exposed capability, then
    # fall back to the local projector (the pre-existing behaviour).
    explicit = getattr(provider, "supports_vision", None)
    if explicit is not None:
        return bool(explicit)
    return _server_has_vision(server)


class ProviderRegistry:
    """Registry of named model providers."""

    def __init__(self):
        self._providers: dict[str, BaseProvider] = {}

    def register(self, provider: BaseProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> BaseProvider:
        if name not in self._providers:
            raise KeyError(f"Unknown provider: {name!r}. Available: {list(self._providers)}")
        return self._providers[name]

    def get_or(self, name: str, fallback: Optional[BaseProvider] = None) -> Optional[BaseProvider]:
        try:
            return self.get(name)
        except KeyError:
            return fallback

    def list(self) -> list[dict]:
        return [
            {
                "name": p.name,
                "supports_function_calling": bool(getattr(p, "supports_function_calling", False)),
                "supports_streaming": p.supports_streaming,
                "allow_tools": bool(getattr(p, "allow_tools", True)),
                "is_local": bool(getattr(p, "is_local", p.name == "local")),
                "supports_vision": bool(getattr(p, "supports_vision", False)),
            }
            for p in self._providers.values()
        ]

    def __contains__(self, name: str) -> bool:
        return name in self._providers


def create_providers(
    server: Optional[LlamaServer] = None,
    provider_configs: Optional[list[dict]] = None,
    enable_thinking: bool = False,
) -> ProviderRegistry:
    """Factory: create a ProviderRegistry from a list of provider configs.

    Each config dict has: { provider, apiKey, baseUrl, model }.
    All API providers use OpenAIProvider (OpenAI-compatible API).
    The "local" provider is always created if a server is provided.
    """
    registry = ProviderRegistry()

    if server:
        local = LocalProvider(server, enable_thinking=enable_thinking)
        registry.register(local)

    for cfg in (provider_configs or []):
        name = cfg.get("provider", "")
        if not name or name == "local":
            continue
        api_key = cfg.get("apiKey", "")
        base_url = cfg.get("baseUrl", "https://api.openai.com/v1")
        model = cfg.get("model", "gpt-4o")
        if not api_key:
            api_key = os.environ.get(f"PET_{name.upper()}_KEY", "")
        if not api_key:
            continue  # skip providers without API key

        local_endpoint = is_local_url(base_url)
        _function_calling = cfg.get(
            "functionCalling", cfg.get("supportsFunctionCalling")
        )
        if not isinstance(_function_calling, bool):
            _function_calling = None
        openai = OpenAIProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
            name=name,
            timeout=300.0 if local_endpoint else 60.0,
            is_local=local_endpoint,
            allow_tools=bool(cfg.get("toolsEnabled", not local_endpoint)),
            supports_function_calling=_function_calling,
            supports_vision=vision_capability_from_config(cfg),
        )
        registry.register(openai)

    return registry
