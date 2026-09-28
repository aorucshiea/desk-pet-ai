"""ControlTagFilter — WALK body-command support tests.

The filter strips [WALK:dx,dy] / [WALK_DESKTOP] from the display stream
and surfaces them via on_control (the stream loop turns them into
body-command SSE events). EMOTION/NEXT_CHAT behavior must stay intact.
"""

from __future__ import annotations

from gateway.think_filter import ControlTagFilter


def _collect():
    events = []

    def on_control(kind, dx, dy):
        events.append((kind, dx, dy))

    return events, ControlTagFilter(on_control=on_control)


def test_walk_tag_is_stripped_and_surfaced():
    events, f = _collect()
    out = f.feed("你好呀[WALK:120,-40]我想去那边看看") + f.flush()
    assert out == "你好呀我想去那边看看"
    assert events == [("walk", 120, -40)]


def test_walk_desktop_tag():
    events, f = _collect()
    out = f.feed("去另一个桌面看看[WALK_DESKTOP]")
    assert "WALK" not in out
    assert events == [("walk_desktop", None, None)]


def test_walk_tag_split_across_deltas():
    events, f = _collect()
    o1 = f.feed("走[WAL")
    o2 = f.feed("K:50,60]")
    assert "WAL" not in o1 and "WAL" not in o2
    assert events == [("walk", 50, 60)]


def test_multiple_walks():
    events, f = _collect()
    f.feed("[WALK:10,0]先走一步[WALK:0,20]再走一步[WALK_DESKTOP]")
    assert events == [
        ("walk", 10, 0),
        ("walk", 0, 20),
        ("walk_desktop", None, None),
    ]


def test_emotion_and_next_chat_untouched():
    events, f = _collect()
    out = f.feed("好开心[EMOTION:happy][NEXT_CHAT:300]")
    assert out == "好开心"
    assert events == []  # EMOTION/NEXT_CHAT are NOT surfaced to on_control


def test_bracket_text_not_confused_with_walk():
    events, f = _collect()
    out = f.feed("这是一个普通括号 [参见说明] 结束")
    assert out == "这是一个普通括号 [参见说明] 结束"
    assert events == []
