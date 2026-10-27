"""Local-endpoint behaviour: loopback detection, output budget, timeout.

LM Studio / llama.cpp / Ollama sit behind the same OpenAI-compatible wire
format as a cloud API, but their economics are different: the user pays for
prefill in wall-clock time. These tests pin the two regressions that made a
2B local model look like it "suddenly stopped":

* the gateway bumped every non-``local`` provider to 8192 output tokens,
  which made LM Studio allocate ``prompt + 8192`` of context;
* nothing distinguished ``http://127.0.0.1:1234/v1`` from a cloud endpoint,
  so the HTTP read timeout stayed at the cloud-sized 60 s.
"""

from __future__ import annotations

from gateway.providers.openai import OpenAIProvider, is_local_url
from gateway.server import ChatRequest, _output_budget


class _FakeProvider:
    def __init__(self, name: str, is_local: bool) -> None:
        self.name = name
        self.is_local = is_local


def _req(max_new_tokens: int = 768, thinking: bool = False) -> ChatRequest:
    return ChatRequest(
        messages=[{"role": "user", "content": "你好"}],
        max_new_tokens=max_new_tokens,
        thinking=thinking,
    )


def test_is_local_url_recognises_loopback_forms():
    assert is_local_url("http://127.0.0.1:1234/v1")
    assert is_local_url("http://localhost:11434/v1")
    assert is_local_url("http://[::1]:8080/v1")
    assert is_local_url("http://0.0.0.0:8000/v1")


def test_is_local_url_rejects_remote_endpoints():
    assert not is_local_url("https://api.deepseek.com/v1")
    assert not is_local_url("https://api.siliconflow.cn/v1")
    assert not is_local_url("")


def test_provider_detects_local_base_url():
    local = OpenAIProvider(base_url="http://127.0.0.1:1234/v1", model="minicpm5-2b")
    cloud = OpenAIProvider(base_url="https://api.deepseek.com/v1", model="deepseek")
    assert local.is_local is True
    assert cloud.is_local is False


def test_provider_allows_explicit_is_local_override():
    # e.g. LM Studio exposed through a LAN address / SSH tunnel — still the
    # user's own GPU, still needs the local timeout and output budget.
    p = OpenAIProvider(base_url="http://192.168.1.20:1234/v1", model="x", is_local=True)
    assert p.is_local is True


def test_local_output_budget_keeps_renderer_value():
    provider = _FakeProvider("lmstudio", is_local=True)
    assert _output_budget(provider, _req(768)) == 768
    assert _output_budget(provider, _req(1280, thinking=True)) == 1280


def test_cloud_output_budget_keeps_8192_floor():
    provider = _FakeProvider("deepseek", is_local=False)
    assert _output_budget(provider, _req(768)) == 8192


def test_builtin_local_provider_keeps_renderer_value():
    provider = _FakeProvider("local", is_local=False)
    assert _output_budget(provider, _req(768)) == 768


def test_provider_tools_default_on_for_direct_construction():
    # Direct construction keeps the old default; the server registration
    # turns tools off for loopback base URLs (providers.json can opt back
    # in with "toolsEnabled": true).
    p = OpenAIProvider(base_url="http://127.0.0.1:1234/v1", model="minicpm5-2b")
    assert p.allow_tools is True


def test_provider_allow_tools_can_be_disabled():
    p = OpenAIProvider(
        base_url="http://127.0.0.1:1234/v1", model="minicpm5-2b", allow_tools=False
    )
    assert p.allow_tools is False


def test_provider_function_calling_can_be_disabled():
    # A browser-driven web-chat bridge speaks the OpenAI wire format but has
    # no native tool calls; the renderer must keep the text-MCP prompt.
    p = OpenAIProvider(
        base_url="http://127.0.0.1:8766/v1",
        model="deepseek-web",
        supports_function_calling=False,
    )
    assert p.supports_function_calling is False
    assert OpenAIProvider(base_url="https://api.deepseek.com/v1").supports_function_calling is True
