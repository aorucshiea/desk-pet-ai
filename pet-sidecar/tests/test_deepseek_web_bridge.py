"""The DeepSeek web bridge's prompt flattening and extension guards (no browser)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from deepseek_web import bridge


@pytest.fixture(autouse=True)
def _fresh_web_session(monkeypatch):
    monkeypatch.setattr(bridge, "SESSION", {
        "active": False,
        "fingerprints": [],
        "page": "",
        "turns": 0,
        "chars": 0,
    })
    monkeypatch.setattr(bridge, "LAST_PAGE", "")


def test_flatten_messages_keeps_system_and_roles():
    text = bridge.flatten_messages({
        "messages": [
            {"role": "system", "content": "你是麻薯"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
            {"role": "tool", "content": "{\"ok\": true}"},
        ]
    })
    assert "【系统设定】\n你是麻薯" in text
    assert "【用户】\n你好" in text
    assert "【助手】\n你好呀" in text
    assert "【工具结果】" in text
    assert text.endswith("不要输出思考过程。")


def test_flatten_messages_accepts_content_blocks_and_caps_size():
    original = bridge.MAX_PROMPT_CHARS
    try:
        bridge.MAX_PROMPT_CHARS = 120
        text = bridge.flatten_messages({
            "system": "S" * 200,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "U" * 200},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]}],
        })
        assert len(text) == 120
        assert "base64" not in text
    finally:
        bridge.MAX_PROMPT_CHARS = original


# ---------------------------------------------------------------------------
# Image extraction and image-only jobs
# ---------------------------------------------------------------------------

def _image_part(data: str, mime: str = "image/jpeg") -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def test_extract_images_parses_openai_and_plain_variants():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "看看这两张"},
        _image_part("QUJD", "image/jpeg"),
        {"type": "image", "url": "data:image/png;base64,REVG"},
    ]}]}

    assert bridge.extract_images(body) == [
        {"mime": "image/jpeg", "base64": "QUJD"},
        {"mime": "image/png", "base64": "REVG"},
    ]


def test_extract_images_strips_whitespace_and_skips_bad_or_oversized(monkeypatch):
    monkeypatch.setattr(bridge, "MAX_IMAGE_BASE64_CHARS", 8)
    content = [
        _image_part("QU\nJD"),
        _image_part("QUJDREVGR0g=", "image/webp"),  # 12 chars > cap
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
        {"type": "image_url", "image_url": {"url": "data:text/plain;base64,QUJD"}},
        {"type": "image_url", "image_url": {"url": "garbage"}},
        {"type": "text", "text": "纯文本"},
    ]

    assert bridge.extract_images({"messages": [{"role": "user", "content": content}]}) == [
        {"mime": "image/jpeg", "base64": "QUJD"},
    ]


def test_extract_images_caps_at_four():
    content = [_image_part(f"AAAA{i}", "image/png") for i in range(6)]

    images = bridge.extract_images({"messages": [{"role": "user", "content": content}]})

    assert len(images) == 4
    assert [item["base64"] for item in images] == ["AAAA0", "AAAA1", "AAAA2", "AAAA3"]


def test_flatten_messages_keeps_text_and_marks_attached_image():
    text = bridge.flatten_messages({"messages": [{"role": "user", "content": [
        {"type": "text", "text": "你看"},
        _image_part("QUJDREVGR0hJSktMTU5PUA=="),
        {"type": "text", "text": "这张图"},
    ]}]})

    assert "【用户】\n你看 这张图\n" + bridge.IMAGE_PLACEHOLDER in text
    assert "QUJDREVGR0hJSktMTU5PUA==" not in text


def test_flatten_messages_keeps_image_only_message():
    text = bridge.flatten_messages({"messages": [{"role": "user", "content": [
        _image_part("QUJDREVGR0hJSktMTU5PUA=="),
    ]}]})

    assert "【用户】\n" + bridge.IMAGE_PLACEHOLDER in text
    assert "QUJDREVGR0hJSktMTU5PUA==" not in text


def test_build_job_accepts_image_only_request():
    job = bridge.build_job({"messages": [{"role": "user", "content": [
        _image_part("QUJDREVGR0hJSktMTU5PUA=="),
    ]}]})

    assert job is not None
    assert job["images"] == [
        {"mime": "image/jpeg", "base64": "QUJDREVGR0hJSktMTU5PUA=="},
    ]
    assert bridge.IMAGE_PLACEHOLDER in job["prompt"]
    assert "QUJDREVGR0hJSktMTU5PUA==" not in job["prompt"]
    assert job["new_chat"] is True
    assert job["id"].startswith("chatcmpl-")


def test_build_job_text_only_has_empty_images():
    job = bridge.build_job({"messages": [{"role": "user", "content": "你好"}]})

    assert job is not None
    assert job["images"] == []
    assert bridge.IMAGE_PLACEHOLDER not in job["prompt"]


def _commit(job: dict, page: str = "") -> None:
    bridge._commit_session(job.pop("_session"), page)


def test_same_conversation_sends_system_only_on_first_turn():
    first = bridge.build_job({
        "system": "SYSTEM-CONTRACT",
        "messages": [{"role": "user", "content": "第一句"}],
    })
    assert first["new_chat"] is True
    assert "SYSTEM-CONTRACT" in first["prompt"]
    _commit(first, "https://chat.deepseek.com/chat/s/abc")

    second = bridge.build_job({
        "system": "SYSTEM-CONTRACT-CHANGED",
        "messages": [
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "第一句的回答"},
            {"role": "user", "content": "第二句"},
        ],
    })
    assert second["new_chat"] is False
    assert "SYSTEM-CONTRACT" not in second["prompt"]
    assert "第一句的回答" not in second["prompt"]
    assert "【用户】\n第二句" in second["prompt"]


def test_same_conversation_sends_only_tool_result_after_tool_call():
    first = bridge.build_job({
        "system": "SYSTEM",
        "messages": [{"role": "user", "content": "看看屏幕"}],
    })
    _commit(first)
    second = bridge.build_job({
        "system": "SYSTEM",
        "messages": [
            {"role": "user", "content": "看看屏幕"},
            {"role": "assistant", "content": "[MCP:builtin/capture_screen:{}]"},
            {"role": "user", "content": "[capture_screen 结果]\\n一张截图"},
        ],
    })
    assert second["new_chat"] is False
    assert "【工具结果】" not in second["prompt"]  # role is user for text providers
    assert "capture_screen 结果" in second["prompt"]
    assert "看看屏幕" not in second["prompt"]


def test_repeated_identical_life_turns_send_only_the_last_turn():
    """Proactive rounds reuse the same text; that must not resend the prompt.

    The gateway trims history to the last 8 messages and every life tick says
    exactly "生命在继续". With identical fingerprints suffix alignment either
    swallows the new turn (empty delta -> full prompt) or resends all 8.
    """
    turn = {"role": "user", "content": "生命在继续"}
    first = bridge.build_job({
        "system": "SYSTEM-CONTRACT",
        "messages": [turn] * 8,
    })
    assert first["new_chat"] is True
    assert "SYSTEM-CONTRACT" in first["prompt"]
    assert first["prompt"].count("【用户】") == 1
    assert first["prompt"].count("生命在继续") == 1
    _commit(first, "https://chat.deepseek.com/chat/s/life")

    for expected_turns in (2, 3):
        job = bridge.build_job({
            "system": "SYSTEM-CONTRACT",
            "messages": [turn] * 8,
        })
        assert job["new_chat"] is False
        assert "SYSTEM-CONTRACT" not in job["prompt"]
        assert job["prompt"].count("【用户】") == 1
        assert job["prompt"].count("生命在继续") == 1
        assert job["_session"]["turns"] == expected_turns
        _commit(job, "https://chat.deepseek.com/chat/s/life")


def test_session_resets_when_page_changes_or_turns_cap():
    first = bridge.build_job({
        "system": "SYSTEM",
        "messages": [{"role": "user", "content": "第一句"}],
    })
    _commit(first, "https://chat.deepseek.com/chat/s/abc")
    bridge.LAST_PAGE = "https://chat.deepseek.com/chat/s/new"
    changed = bridge.build_job({
        "system": "SYSTEM",
        "messages": [{"role": "user", "content": "新会话"}],
    })
    assert changed["new_chat"] is True
    assert "SYSTEM" in changed["prompt"]


def test_no_tools_ephemeral_turn_does_not_become_session_base():
    normal = bridge.build_job({
        "system": "SYSTEM-WITH-TOOLS",
        "messages": [{"role": "user", "content": "正常"}],
    })
    _commit(normal, "https://chat.deepseek.com/chat/s/abc")

    boot = bridge.build_job({
        "system": "BOOT-NO-TOOLS",
        "messages": [{"role": "user", "content": "醒来"}],
        "tools_enabled": False,
    })
    assert boot["new_chat"] is True
    assert boot["_session"]["commit"] is False
    _commit(boot, "https://chat.deepseek.com/chat/s/boot")
    assert bridge.SESSION["active"] is False

    after = bridge.build_job({
        "system": "SYSTEM-WITH-TOOLS",
        "messages": [{"role": "user", "content": "正常第二轮"}],
    })
    assert after["new_chat"] is True
    assert "SYSTEM-WITH-TOOLS" in after["prompt"]


def test_session_reset_endpoint(bridge_server):
    bridge.SESSION.update({
        "active": True, "fingerprints": ["x"], "page": "p", "turns": 3, "chars": 100,
    })
    status, payload = _request(bridge_server, "POST", "/session/reset", {})
    assert status == 200
    assert payload["session_active"] is False
    assert bridge.SESSION["active"] is False


def test_chat_completions_queues_image_only_job_once(bridge_server, monkeypatch):
    counter_calls: list[int] = []
    monkeypatch.setattr(bridge, "_bump_message_counter", lambda: counter_calls.append(1))
    monkeypatch.setattr(bridge, "LAST_POLL_AT", time.time())
    outcome: dict = {}

    def call():
        outcome["value"] = _request(bridge_server, "POST", "/v1/chat/completions", {
            "messages": [{"role": "user", "content": [
                _image_part("QUJDREVGR0hJSktMTU5PUA=="),
            ]}],
        })

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    job = bridge.JOBS.get(timeout=2)

    assert job["images"] == [
        {"mime": "image/jpeg", "base64": "QUJDREVGR0hJSktMTU5PUA=="},
    ]
    assert bridge.IMAGE_PLACEHOLDER in job["prompt"]
    assert "QUJDREVGR0hJSktMTU5PUA==" not in job["prompt"]
    assert job["id"] in bridge.PENDING
    assert counter_calls == [1]
    assert bridge.JOBS.qsize() == 0

    status, _ = _request(
        bridge_server, "POST", f"/jobs/{job['id']}/result", {"text": "看到了"}
    )
    assert status == 200
    thread.join(timeout=5)
    assert not thread.is_alive()
    status, payload = outcome["value"]
    assert status == 200
    assert payload["choices"][0]["message"]["content"] == "看到了"
    assert bridge.PENDING == {}


# ---------------------------------------------------------------------------
# Extension connection guard
# ---------------------------------------------------------------------------

def test_extension_connected_false_when_never_polled(monkeypatch):
    monkeypatch.setattr(bridge, "LAST_POLL_AT", 0.0)
    assert bridge.extension_connected(now=10_000.0) is False
    assert bridge._reject_if_no_extension() == bridge.EXTENSION_NOT_CONNECTED_MESSAGE


def test_extension_connected_false_when_poll_is_old(monkeypatch):
    monkeypatch.setattr(bridge, "LAST_POLL_AT", 1_000.0)
    assert bridge.extension_connected(now=1_000.0 + 12.1) is False
    assert bridge.extension_connected(now=1_000.0 + 60.0) is False


def test_extension_connected_true_when_poll_is_recent(monkeypatch):
    monkeypatch.setattr(bridge, "LAST_POLL_AT", 1_000.0)
    assert bridge.extension_connected(now=1_000.5) is True
    assert bridge.extension_connected(now=1_000.0 + 12.0) is True
    assert bridge.extension_connected(now=1_000.0 + 11.9, max_age=12.0) is True
    assert bridge.extension_connected(now=1_000.0 + 11.9, max_age=10.0) is False
    monkeypatch.setattr(bridge, "LAST_POLL_AT", time.time())
    assert bridge._reject_if_no_extension() is None


@pytest.fixture
def bridge_state(monkeypatch):
    """Reset mutable module globals and clear the queues around a test."""
    monkeypatch.setattr(bridge, "LAST_POLL_AT", 0.0)
    monkeypatch.setattr(bridge, "LAST_RESULT_AT", 0.0)
    monkeypatch.setattr(bridge, "COMPLETED_JOBS", 0)
    monkeypatch.setattr(bridge, "MESSAGES_TODAY", 0)
    monkeypatch.setattr(bridge, "MESSAGES_DAY", "")
    with bridge.LOCK:
        bridge.PENDING.clear()
    while not bridge.JOBS.empty():
        bridge.JOBS.get_nowait()
    yield
    with bridge.LOCK:
        bridge.PENDING.clear()
    while not bridge.JOBS.empty():
        bridge.JOBS.get_nowait()


@pytest.fixture
def bridge_server(bridge_state):
    server = bridge.ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _request(server, method: str, path: str, payload: dict | None = None):
    url = "http://127.0.0.1:%d%s" % (server.server_address[1], path)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def test_chat_completions_fails_fast_without_extension(bridge_server, monkeypatch):
    counter_calls: list[int] = []
    monkeypatch.setattr(bridge, "_bump_message_counter", lambda: counter_calls.append(1))

    status, payload = _request(bridge_server, "POST", "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "你好"}],
    })

    assert status == 503
    assert payload == {"error": {"message": bridge.EXTENSION_NOT_CONNECTED_MESSAGE}}
    assert counter_calls == []
    assert bridge.MESSAGES_TODAY == 0
    assert bridge.PENDING == {}
    assert bridge.JOBS.qsize() == 0


def test_chat_completions_fails_fast_when_poll_is_stale(bridge_server, monkeypatch):
    monkeypatch.setattr(bridge, "LAST_POLL_AT", time.time() - 60.0)

    status, payload = _request(bridge_server, "POST", "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "你好"}],
    })

    assert status == 503
    assert payload["error"]["message"] == bridge.EXTENSION_NOT_CONNECTED_MESSAGE
    assert bridge.PENDING == {}
    assert bridge.JOBS.qsize() == 0


def test_health_reports_extension_connected_and_last_poll(bridge_server, monkeypatch):
    monkeypatch.setattr(bridge, "LAST_POLL_AT", time.time() - 3.0)

    status, payload = _request(bridge_server, "GET", "/health")

    assert status == 200
    assert payload["extension_connected"] is True
    assert payload["last_poll_seconds"] is not None
    assert payload["last_poll_seconds"] >= 2.0


def test_daily_limit_env_parses_and_tolerates_garbage(monkeypatch):
    monkeypatch.setenv("PET_WEB_DAILY_LIMIT", "42")
    assert bridge._daily_limit_from_env() == 42
    monkeypatch.setenv("PET_WEB_DAILY_LIMIT", "nope")
    assert bridge._daily_limit_from_env() == 100
    monkeypatch.setenv("PET_WEB_DAILY_LIMIT", "0")
    assert bridge._daily_limit_from_env() == 0


def test_chat_completions_rejects_when_daily_limit_reached(bridge_server, monkeypatch):
    counter_calls: list[int] = []
    monkeypatch.setattr(bridge, "_bump_message_counter", lambda: counter_calls.append(1))
    monkeypatch.setattr(bridge, "LAST_POLL_AT", time.time())
    monkeypatch.setattr(bridge, "MESSAGES_TODAY", 7)
    monkeypatch.setattr(bridge, "DAILY_LIMIT", 7)

    status, payload = _request(bridge_server, "POST", "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "第 8 条"}],
    })

    assert status == 429
    assert "已达上限" in payload["error"]["message"]
    assert counter_calls == []
    assert bridge.MESSAGES_TODAY == 7
    assert bridge.PENDING == {}
    assert bridge.JOBS.qsize() == 0


def test_health_reports_daily_limit_and_remaining(bridge_server, monkeypatch):
    monkeypatch.setattr(bridge, "LAST_POLL_AT", time.time())
    monkeypatch.setattr(bridge, "MESSAGES_TODAY", 6)
    monkeypatch.setattr(bridge, "DAILY_LIMIT", 100)

    status, payload = _request(bridge_server, "GET", "/health")

    assert status == 200
    assert payload["messages_today"] == 6
    assert payload["daily_limit"] == 100
    assert payload["remaining"] == 94


def test_config_endpoint_updates_limit_and_persists(bridge_server, monkeypatch, tmp_path):
    config_path = tmp_path / "deepseek-web-config.json"
    monkeypatch.setattr(bridge, "CONFIG_PATH", str(config_path))
    monkeypatch.setattr(bridge, "DAILY_LIMIT", 100)

    status, payload = _request(bridge_server, "POST", "/config", {"daily_limit": 12})

    assert status == 200
    assert payload == {"ok": True, "daily_limit": 12}
    assert bridge.DAILY_LIMIT == 12
    assert json.loads(config_path.read_text("utf-8")) == {"daily_limit": 12}

    status, payload = _request(bridge_server, "GET", "/health")
    assert status == 200
    assert payload["daily_limit"] == 12
    assert payload["remaining"] == 12

    status, _ = _request(bridge_server, "POST", "/config", {"daily_limit": 0})
    assert status == 200
    assert bridge.DAILY_LIMIT == 0
    status, health = _request(bridge_server, "GET", "/health")
    assert status == 200
    assert health["remaining"] is None


def test_config_endpoint_rejects_invalid_limits(bridge_server, monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "CONFIG_PATH", str(tmp_path / "deepseek-web-config.json"))
    monkeypatch.setattr(bridge, "DAILY_LIMIT", 100)

    for bad in (-1, 10_001, "abc", True, None, 1.5):
        status, payload = _request(bridge_server, "POST", "/config", {"daily_limit": bad})
        assert status == 400
        assert "daily_limit" in payload["error"]["message"]
        assert bridge.DAILY_LIMIT == 100
