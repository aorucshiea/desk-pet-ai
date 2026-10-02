"""Lock down ThinkBlockFilter behaviour ported from the legacy bridge.

These tests are the protocol between Electron and the gateway: the renderer
(clawd-on-desk/src/pet-chat.html) relies on these exact event shapes
to switch between the "think" and the "reply" bubbles.
"""

from __future__ import annotations

from gateway.think_filter import ThinkBlockFilter, ToolNarrationFilter


def _events(filter_: ThinkBlockFilter, pieces: list[str]) -> list[dict]:
    out: list[dict] = []
    for p in pieces:
        out.extend(filter_.feed(p))
    out.extend(filter_.flush())
    return out


def test_plain_text_only():
    f = ThinkBlockFilter(expose=False)
    assert _events(f, ["hello ", "world"]) == [
        {"event": "delta", "content": "hello "},
        {"event": "delta", "content": "world"},
    ]


def test_think_exposed():
    f = ThinkBlockFilter(expose=True)
    events = _events(f, ["<think>reasoning</think>\n\nactual"])
    assert events == [
        {"event": "think", "content": "reasoning"},
        {"event": "delta", "content": "actual"},
    ]


def test_think_hidden_drops_reasoning():
    f = ThinkBlockFilter(expose=False)
    events = _events(f, ["<think>secret</think>\n\nuser-visible"])
    assert events == [{"event": "delta", "content": "user-visible"}]


def test_open_tag_split_across_chunks():
    f = ThinkBlockFilter(expose=True)
    events = _events(f, ["text <thi", "nk>raw</think>\n\nbody"])
    assert events == [
        {"event": "delta", "content": "text "},
        {"event": "think", "content": "raw"},
        {"event": "delta", "content": "body"},
    ]


def test_close_tag_split_across_chunks():
    f = ThinkBlockFilter(expose=True)
    events = _events(f, ["<think>a</thi", "nk>b"])
    assert events == [
        {"event": "think", "content": "a"},
        {"event": "delta", "content": "b"},
    ]


def test_start_inside_thinking():
    f = ThinkBlockFilter(expose=True, start_inside=True)
    events = _events(f, ["raw reasoning</think>\n\nreply"])
    assert events == [
        {"event": "think", "content": "raw reasoning"},
        {"event": "delta", "content": "reply"},
    ]


def test_trailing_partial_tag_flushed_as_delta():
    # When the stream ends mid-tag we must still flush whatever is in the
    # buffer so the user doesn't lose tokens — even if it looks like a
    # half-open <think>.
    f = ThinkBlockFilter(expose=True)
    events = _events(f, ["hello <thi"])
    assert events == [
        {"event": "delta", "content": "hello "},
        {"event": "delta", "content": "<thi"},
    ]


class TestToolNarrationFilter:
    """The tool-loop models narrate their own tool calls ("_执行 recall..._")
    as markdown asides — those lines are pipeline chatter, not pet speech."""

    def _collect(self, pieces: list[str]) -> str:
        f = ToolNarrationFilter()
        out = "".join(f.feed(p) for p in pieces)
        return out + f.flush()

    def test_drops_narration_lines(self):
        text = "你好呀。\n\n_执行 recall..._\n\n_执行 pet_mood_now..._\n\n我今天很好。\n"
        assert self._collect([text]) == "你好呀。\n\n\n\n我今天很好。\n"

    def test_survives_mid_line_chunk_splits(self):
        pieces = ["前文\n\n_执", "行 re", "call..._\n\n后文\n"]
        assert self._collect(pieces) == "前文\n\n\n后文\n"

    def test_flush_carries_unclosed_line(self):
        assert self._collect(["结尾没有换行"]) == "结尾没有换行"

    def test_keeps_normal_text(self):
        text = "_这个_词有强调\n执行任务很重要\n"
        out = self._collect([text])
        assert "强调" in out and "执行任务很重要" in out
