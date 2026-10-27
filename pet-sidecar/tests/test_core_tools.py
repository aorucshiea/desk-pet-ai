"""Soul tools must survive the loopback-provider tool budget cut.

External loopback providers (LM Studio / Ollama / an attached llama.cpp)
default to ``allow_tools=False`` because 30+ tool schemas are expensive to
prefill on a small local model. That switch is about heavy/optional tools,
not about amputating the pet's memory: ``memory``, ``recall`` and
``memory_search`` stay available, through native function calling or the
text ``[MCP:...]`` fallback.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from gateway import server as server_mod
from gateway.providers import ProviderRegistry


class _FakeMcp:
    def __init__(self, tools: list[dict]) -> None:
        self._tools = tools
        self.called: list[tuple[str, str, dict]] = []

    async def list_all_tools(self):
        return [dict(t) for t in self._tools]

    async def call_tool(self, server_name, tool_name, arguments):
        self.called.append((server_name, tool_name, arguments))
        return {"summary": f"{tool_name} ok", "content": []}


class _FakeKernel:
    def all_tools(self):
        return {
            "recall": {"description": "recall", "parameters": {}},
            "memory_search": {"description": "search", "parameters": {}},
            "pet_forge_plugin": {"description": "forge", "parameters": {}},
        }


def _tool(server_name: str, name: str) -> dict:
    return {
        "server_name": server_name,
        "name": name,
        "description": name,
        "input_schema": {"type": "object", "properties": {}},
    }


def _req(tools_enabled: bool = True) -> server_mod.ChatRequest:
    return server_mod.ChatRequest(
        messages=[{"role": "user", "content": "你还记得吗"}],
        tools_enabled=tools_enabled,
    )


def test_core_tool_names_are_the_soul_tools_plus_decision_pair():
    assert server_mod.CORE_TOOL_NAMES == {
        "memory",
        "recall",
        "memory_search",
        "pet_decision_roll",
        "pet_override_decision",
        "pet_enter_slow",
        "pet_decision",
    }


def test_gather_tools_core_only_keeps_soul_tools_and_sheds_heavy_ones(monkeypatch):
    mcp = _FakeMcp([
        _tool("builtin", "memory"),
        _tool("builtin", "desktop_task"),
        _tool("extra", "observe_screen"),
    ])
    monkeypatch.setattr(server_mod, "_kernel", lambda: _FakeKernel())
    monkeypatch.setattr(server_mod, "_intuition_registry_ref", object())

    core = asyncio.run(server_mod._gather_tools(mcp, _req(), core_only=True))
    core_names = [t.name for t in core]
    assert set(core_names) == {"memory", "recall", "memory_search"}
    # Matching is by tool name only — a core tool served by a plugin (pet)
    # or by an MCP server must both survive.
    assert {t.server_name for t in core} == {"builtin", "pet"}
    assert "desktop_task" not in core_names
    assert "pet_world_bodies" not in core_names
    assert "pet_add_intuition" not in core_names

    full = asyncio.run(server_mod._gather_tools(mcp, _req()))
    full_names = {t.name for t in full}
    assert {"memory", "recall", "memory_search"} <= full_names
    assert {
        "desktop_task",
        "observe_screen",
        "pet_world_bodies",
        "pet_world_operate",
        "pet_add_intuition",
        "pet_forge_plugin",
    } <= full_names, "the full tool table must not be trimmed"


def test_gather_tools_core_only_respects_tools_enabled(monkeypatch):
    monkeypatch.setattr(server_mod, "_kernel", lambda: None)
    mcp = _FakeMcp([_tool("builtin", "memory")])
    assert asyncio.run(
        server_mod._gather_tools(mcp, _req(tools_enabled=False), core_only=True)
    ) is None


class _Bridge:
    def new_session(self) -> None: ...

    def post(self, *_a, **_k) -> None: ...


class _Mood:
    emotion_index = 0.0

    def format_for_system_prompt(self) -> str:
        return ""

    def apply_emotion_tag(self, _tag) -> None: ...


class _Registry:
    def __init__(self, provider) -> None:
        self._providers = {provider.name: provider}
        self._default = provider

    def get(self, name):
        return self._providers.get(name, self._default)


def _run_chat(provider, mcp, monkeypatch) -> list[bytes]:
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(server_mod, "_kernel", lambda: None)
    monkeypatch.setattr(server_mod, "get_event_store", lambda: None)
    monkeypatch.setattr(server_mod, "_extraction_hook", None)

    async def _drain():
        return [
            chunk
            async for chunk in server_mod._stream_chat_provider(
                _Registry(provider),
                mcp,
                _Bridge(),
                _req(),
                server=None,
                server_state={},
                mood_store=_Mood(),
                lora_arr=None,
            )
        ]

    return asyncio.run(_drain())


class _TextProvider:
    """Loopback endpoint: no native function calling, tools off by policy."""

    name = "lmstudio"
    is_local = True
    allow_tools = False
    supports_function_calling = False

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            yield {
                "type": "delta",
                "content": (
                    '我记下来了。\n'
                    '[MCP:builtin/memory:{"action":"add","target":"memory","content":"用户喜欢猫"}]\n'
                    '[MCP:builtin/desktop_task:{"task":"打开记事本"}]'
                ),
            }
            yield {"type": "end"}
        else:
            yield {"type": "delta", "content": "记好了。"}
            yield {"type": "end"}


class _NativeProvider:
    """Loopback endpoint with native function calling, heavy tools off."""

    name = "lmstudio"
    is_local = True
    allow_tools = False
    supports_function_calling = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            yield {
                "type": "tool_call",
                "id": "call_memory",
                "server_name": "default",
                "name": "mcp_builtin_memory",
                "arguments": {"action": "add", "content": "用户喜欢猫"},
            }
            yield {
                "type": "tool_call",
                "id": "call_heavy",
                "server_name": "default",
                "name": "mcp_builtin_desktop_task",
                "arguments": {"task": "打开记事本"},
            }
            yield {"type": "end"}
        else:
            yield {"type": "delta", "content": "记好了。"}
            yield {"type": "end"}


def test_core_only_text_mcp_call_reaches_execution(monkeypatch):
    provider = _TextProvider()
    mcp = _FakeMcp([_tool("builtin", "memory"), _tool("builtin", "desktop_task")])

    _run_chat(provider, mcp, monkeypatch)

    assert mcp.called == [
        ("builtin", "memory", {"action": "add", "target": "memory", "content": "用户喜欢猫"})
    ], "the core memory call must execute, the heavy desktop_task call must not"
    assert len(provider.calls) >= 2, "the pet must continue after the tool result"
    assert {t.name for t in provider.calls[0]["tools"]} == {"memory"}


def test_core_only_native_tool_call_reaches_execution(monkeypatch):
    provider = _NativeProvider()
    mcp = _FakeMcp([_tool("builtin", "memory"), _tool("builtin", "desktop_task")])

    _run_chat(provider, mcp, monkeypatch)

    assert [c[1] for c in mcp.called] == ["memory"], (
        "only the core tool call may execute; the heavy one must be ignored"
    )
    assert len(provider.calls) >= 2
    assert {t.name for t in provider.calls[0]["tools"]} == {"memory"}


class _BadMarkerProvider:
    """Loopback endpoint: first turn emits an unparsable [MCP:] marker."""

    name = "lmstudio"
    is_local = True
    allow_tools = False
    supports_function_calling = False

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            yield {"type": "delta", "content": "我查一下。\n[MCP:builtin/memory:list]"}
        else:
            yield {"type": "delta", "content": "好的。"}
        yield {"type": "end"}


class _BadMarkerFullProvider(_BadMarkerProvider):
    allow_tools = True


def _syntax_correction(provider, mcp, monkeypatch) -> str:
    _run_chat(provider, mcp, monkeypatch)
    assert len(provider.calls) >= 2, "the malformed marker must earn a correction retry"
    return provider.calls[1]["messages"][-1]["content"]


def test_core_only_syntax_correction_example_is_a_core_tool(monkeypatch):
    provider = _BadMarkerProvider()
    mcp = _FakeMcp([_tool("builtin", "memory"), _tool("builtin", "capture_screen")])

    correction = _syntax_correction(provider, mcp, monkeypatch)

    assert "builtin/memory" in correction, "the example must be a tool core-only can run"
    assert "capture_screen" not in correction, (
        "capture_screen is filtered out in core-only mode; suggesting it would "
        "make the model repeat a call that is silently ignored"
    )


def test_full_mode_syntax_correction_keeps_capture_screen_example(monkeypatch):
    provider = _BadMarkerFullProvider()
    mcp = _FakeMcp([_tool("builtin", "memory"), _tool("builtin", "capture_screen")])

    correction = _syntax_correction(provider, mcp, monkeypatch)

    assert "capture_screen" in correction


class _NoCapabilityAttrs:
    def __init__(self, name: str) -> None:
        self.name = name
        self.supports_streaming = True


class _LoopbackProvider:
    name = "lmstudio"
    supports_streaming = True
    supports_function_calling = True
    allow_tools = False
    is_local = True


def test_provider_registry_list_adds_capability_fields_with_defaults():
    registry = ProviderRegistry()
    registry.register(_NoCapabilityAttrs("local"))
    registry.register(_NoCapabilityAttrs("deepseek"))
    registry.register(_LoopbackProvider())

    entries = {e["name"]: e for e in registry.list()}

    # Existing fields stay; new ones are additive.
    for entry in entries.values():
        assert {"name", "supports_function_calling", "supports_streaming"} <= set(entry)
        assert {"allow_tools", "is_local"} <= set(entry)

    assert entries["local"]["supports_function_calling"] is False
    assert entries["local"]["allow_tools"] is True
    assert entries["local"]["is_local"] is True
    assert entries["deepseek"]["allow_tools"] is True
    assert entries["deepseek"]["is_local"] is False
    assert entries["lmstudio"]["supports_function_calling"] is True
    assert entries["lmstudio"]["allow_tools"] is False
    assert entries["lmstudio"]["is_local"] is True


def test_providers_endpoint_exposes_capability_fields():
    app = server_mod.build_app(initial_model=None)
    with TestClient(app) as client:
        body = client.get("/api/providers").json()

    assert body["default"] == "local"
    local = next(p for p in body["providers"] if p["name"] == "local")
    assert local["allow_tools"] is True
    assert local["is_local"] is True
    assert local["supports_function_calling"] is False
    assert "supports_streaming" in local
