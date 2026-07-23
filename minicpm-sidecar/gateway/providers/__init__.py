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
from .openai import OpenAIProvider
from .anthropic import AnthropicProvider


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
                "supports_function_calling": p.supports_function_calling,
                "supports_streaming": p.supports_streaming,
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
            api_key = os.environ.get(f"MINICPM_{name.upper()}_KEY", "")
        if not api_key:
            continue  # skip providers without API key

        openai = OpenAIProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
            name=name,
        )
        registry.register(openai)

    return registry
