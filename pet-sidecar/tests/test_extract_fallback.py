from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from fastapi.testclient import TestClient

from gateway import server as server_mod
from gateway.memory import extract_fallback
from gateway.memory.events import EventStore, parse_event_block


class FakeProvider:
    def __init__(self, chunks=(), error=None):
        self.chunks = list(chunks)
        self.error = error
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        for chunk in self.chunks:
            yield {"type": "delta", "content": chunk}
        yield {"type": "end"}


def test_should_fallback_only_for_missing_block_and_real_user_turn():
    assert extract_fallback.should_fallback("这是一段足够长的用户消息", "普通回答")
    assert not extract_fallback.should_fallback("短", "普通回答")
    assert not extract_fallback.should_fallback("[系统提示：该主动说话了]", "普通回答")
    assert not extract_fallback.should_fallback("", "普通回答")
    assert not extract_fallback.should_fallback(
        "这是一段足够长的用户消息", "回答<<<MEM>>>{\"events\":[]}<<<MEMEND>>>"
    )


def test_build_prompt_requests_json_only():
    prompt = extract_fallback.build_prompt("用户原文", "桌宠原文")
    assert "只输出一个 MEM JSON 对象" in prompt
    assert '{"events":[]}' in prompt
    assert "用户原文" in prompt and "桌宠原文" in prompt


def test_build_prompt_caps_input_size_for_cheap_extraction():
    prompt = extract_fallback.build_prompt(
        "u" * 10_000, "r" * 20_000, max_user_chars=10, max_reply_chars=20
    )
    assert "u" * 10 in prompt and "u" * 11 not in prompt
    assert "r" * 20 in prompt and "r" * 21 not in prompt


class _LocalFakeProvider(FakeProvider):
    name = "local"
    is_local = True


def test_local_provider_fallback_is_opt_in(monkeypatch):
    called = []
    provider = _LocalFakeProvider([
        '{"events":[{"title":"兜底","content":"本地提取（平静）","weight":300}]}'
    ])
    monkeypatch.delenv("PET_EXTRACT_FALLBACK_LOCAL", raising=False)
    result = asyncio.run(
        extract_fallback.run_fallback(provider, "今天讨论一下我的散步习惯", "你很喜欢散步。", called.append)
    )
    assert result == {"attempted": False, "applied": False, "reason": "local_provider"}
    assert provider.calls == [] and called == []

    monkeypatch.setenv("PET_EXTRACT_FALLBACK_LOCAL", "1")
    result = asyncio.run(
        extract_fallback.run_fallback(provider, "今天讨论一下我的散步习惯", "你很喜欢散步。", called.append)
    )
    assert result["attempted"] and result["applied"] and result["reason"] == "ok"
    assert len(provider.calls) == 1 and len(called) == 1


def test_run_fallback_wraps_json_and_applies_to_event_store(tmp_path):
    store = EventStore()
    store.load_from_disk(tmp_path)
    seen = []

    def apply(text):
        seen.append(text)
        data = parse_event_block(text)
        added = [store.add_event(**{
            "title": event["title"], "content": event["content"], "weight": event["weight"],
        })["id"] for event in data["events"]]
        return {"events_added": added}

    provider = FakeProvider([
        "```json\n",
        '{"events":[{"title":"兜底记忆","content":"用户喜欢散步（开心）","weight":420}]}',
        "\n```",
    ])
    result = asyncio.run(extract_fallback.run_fallback(provider, "今天讨论一下我的散步习惯", "你很喜欢散步。", apply))

    assert result["attempted"] and result["applied"] and result["reason"] == "ok"
    assert seen[0].startswith("<<<MEM>>>") and seen[0].endswith("<<<MEMEND>>>")
    assert store.get_all_events()[0]["title"] == "兜底记忆"
    kwargs = provider.calls[0]
    assert kwargs["system"] == "" and kwargs["tools"] is None
    assert kwargs["max_tokens"] == 500 and kwargs["temperature"] == 0.2 and kwargs["top_p"] == 0.9


def test_run_fallback_skips_existing_block_and_swallows_errors():
    provider = FakeProvider(error=RuntimeError("offline"))
    called = []
    result = asyncio.run(extract_fallback.run_fallback(
        provider, "这是一段足够长的用户消息", "有<<<MEM>>>{\"events\":[]}<<<MEMEND>>>", called.append,
    ))
    assert result == {"attempted": False, "applied": False, "reason": "not_needed"}
    assert provider.calls == []

    result = asyncio.run(extract_fallback.run_fallback(
        provider, "这是一段足够长的用户消息", "没有记忆块", called.append,
    ))
    assert result["attempted"] and not result["applied"] and result["reason"] == "error"
    assert called == []

    bad_provider = FakeProvider(["not json"])
    result = asyncio.run(extract_fallback.run_fallback(
        bad_provider, "这是一段足够长的用户消息", "没有记忆块", called.append,
    ))
    assert result["reason"] == "unparseable_output" and not result["applied"]


class _MainProvider:
    name = "local"
    supports_function_calling = False
    allow_tools = True

    def __init__(self, text):
        self.text = text

    async def chat(self, **_kwargs):
        yield {"type": "delta", "content": self.text}
        yield {"type": "end"}


class _Registry:
    def __init__(self, provider):
        self._providers = {"local": provider}
        self._default = provider


class _Bridge:
    def new_session(self):
        pass

    def post(self, *_args, **_kwargs):
        pass


class _Mood:
    emotion_index = 0.0

    def format_for_system_prompt(self):
        return ""

    def apply_emotion_tag(self, _tag):
        pass


def test_stream_schedules_fallback_without_blocking_sse(tmp_path, monkeypatch):
    provider = _MainProvider("普通回复，没有记忆块。[NEXT_CHAT:0]")
    scheduled = []

    async def fake_fallback(provider_arg, user, reply, apply, *, timeout=90):
        scheduled.append((provider_arg, user, reply, apply, timeout))
        return {"attempted": True, "applied": True, "reason": "ok"}

    hook = lambda _text: {"ok": True, "events_added": []}
    monkeypatch.setattr(server_mod._extract_fallback, "run_fallback", fake_fallback)
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(server_mod, "_kernel", lambda: None)
    monkeypatch.setattr(server_mod, "get_event_store", lambda: None)
    monkeypatch.setattr(server_mod, "_extraction_hook", hook)
    monkeypatch.setenv("PET_EXTRACT_FALLBACK", "1")
    req = server_mod.ChatRequest(
        messages=[{"role": "user", "content": "这是一段足够长的真实用户消息"}],
        tools_enabled=False,
    )

    async def drain():
        chunks = [chunk async for chunk in server_mod._stream_chat_provider(
            _Registry(provider), object(), _Bridge(), req, None, {}, _Mood(),
            conversation_dir=tmp_path,
        )]
        for _ in range(3):
            await asyncio.sleep(0)
        return chunks

    chunks = asyncio.run(drain())
    assert any(b'"event": "end"' in chunk for chunk in chunks)
    assert len(scheduled) == 1
    provider_arg, user, reply, apply, timeout = scheduled[0]
    assert provider_arg is provider and user.startswith("这是一段")
    assert "普通回复" in reply and apply is hook and timeout == 90


def test_resonance_endpoint_awaits_async_service(monkeypatch):
    class FakeResonance:
        async def find_resonance(self, _store, _message):
            return [{"id": "evt_1", "title": "异步结果", "content": "不是协程"}]

    monkeypatch.setattr(server_mod, "_organ_service", lambda *_args: FakeResonance())
    app = server_mod.build_app(initial_model=None)
    with TestClient(app) as client:
        response = client.post("/api/events/resonance", json={"message": "共振"})

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "resonant": [{"id": "evt_1", "title": "异步结果", "content": "不是协程"}],
        "count": 1,
    }
    assert not inspect.iscoroutine(body["resonant"])
