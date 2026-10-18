"""The gate that keeps narrator/debug traffic out of the daily log.

Narration shares the /api/chat stream (``silent=true``, stream=false). Before
2026-10-19 every narration turn appended a fake "user" prompt and a fake
"assistant" reply to ``conversations/YYYY-MM-DD.jsonl``, poisoning the input
that sleep compression reads.
"""

from __future__ import annotations

from types import SimpleNamespace

from gateway.server import _should_log_conversation


def _req(silent: bool = False, messages=None):
    return SimpleNamespace(
        silent=silent,
        messages=messages if messages is not None else [{"role": "user", "content": "你好"}],
    )


def test_real_user_turn_is_logged(tmp_path):
    assert _should_log_conversation(_req(), tmp_path)


def test_silent_narration_is_not_logged(tmp_path):
    assert not _should_log_conversation(_req(silent=True), tmp_path)


def test_debug_without_conversation_dir_is_not_logged():
    assert not _should_log_conversation(_req(), None)


def test_empty_messages_are_not_logged(tmp_path):
    assert not _should_log_conversation(_req(messages=[]), tmp_path)


def test_last_message_must_be_from_the_user(tmp_path):
    assert not _should_log_conversation(
        _req(messages=[{"role": "assistant", "content": "我先说"}]),
        tmp_path,
    )


def test_proactive_wakeup_is_not_logged(tmp_path):
    assert not _should_log_conversation(
        _req(messages=[{"role": "user", "content": "[系统提示：你已经很久没说话了]"}]),
        tmp_path,
    )


def test_multimodal_user_turn_is_logged(tmp_path):
    content = [{"type": "text", "text": "看看这个"}, {"type": "image_url", "image_url": {}}]
    assert _should_log_conversation(
        _req(messages=[{"role": "user", "content": content}]),
        tmp_path,
    )


def test_pydantic_style_message_objects_work(tmp_path):
    message = SimpleNamespace(role="user", content="hi")
    assert _should_log_conversation(_req(messages=[message]), tmp_path)
