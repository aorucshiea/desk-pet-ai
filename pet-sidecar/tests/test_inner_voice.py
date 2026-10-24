"""心声 ([INNER:…]) tests — the model's own choice of output mode.

心声 = 开口（默认）或 在心里想。When the model wraps its WHOLE reply in
[INNER:…], the content must NEVER reach the display stream (the user sees
nothing — no output, no thinking process); the captured text goes to
ephemeral memory instead (it really thought it, just didn't say it).

These tests pin the ControlTagFilter contract:
- complete tag across arbitrarily-split deltas → zero display leakage,
  on_inner fires once with the content
- unclosed tag at flush → content salvaged to on_inner, raw text suppressed
- normal replies (incl. other control tags) unaffected
"""

from __future__ import annotations

from gateway.think_filter import ControlTagFilter


def _spy():
    inner: list[str] = []
    ctrl: list[tuple] = []
    f = ControlTagFilter(
        on_control=lambda kind, dx, dy: ctrl.append((kind, dx, dy)),
        on_inner=lambda text: inner.append(text),
    )
    return inner, ctrl, f


def test_inner_whole_reply_no_leak_and_captured():
    inner, _, f = _spy()
    out = f.feed("[INNER:今天有点想说点什么，但还是算了]") + f.flush()
    assert out == ""
    assert inner == ["今天有点想说点什么，但还是算了"]


def test_inner_split_across_many_deltas():
    inner, _, f = _spy()
    text = "[INNER:窗外的雨还在下，我想起来一些以前的事，不过现在不是说这些的时候]"
    emitted = []
    # 2-char deltas force mid-tag, mid-content splits
    for i in range(0, len(text), 2):
        emitted.append(f.feed(text[i : i + 2]))
    emitted.append(f.flush())
    assert "".join(emitted) == "", "inner turn must not leak a single char"
    assert inner == ["窗外的雨还在下，我想起来一些以前的事，不过现在不是说这些的时候"]


def test_inner_unclosed_tag_salvaged_at_flush():
    inner, _, f = _spy()
    out = f.feed("[INNER:有点困了，但是还不想睡") + f.flush()
    assert out == "", "dangling [INNER:… must never leak"
    assert inner == ["有点困了，但是还不想睡"]


def test_normal_reply_passthrough_untouched():
    inner, ctrl, f = _spy()
    out = f.feed("我在呢。今天想做什么？") + f.flush()
    assert out == "我在呢。今天想做什么？"
    assert inner == []
    assert ctrl == []


def test_walk_still_works_alongside_inner_support():
    inner, ctrl, f = _spy()
    out = f.feed("走两步[WALK:30,0]") + f.flush()
    assert out == "走两步"
    assert ctrl == [("walk", 30, 0)]
    assert inner == []


def test_inner_with_leading_whitespace():
    inner, _, f = _spy()
    out = f.feed("  [INNER:嗯……]  ") + f.flush()
    assert "INNER" not in out
    assert inner == ["嗯……"]


def test_no_on_inner_callback_is_safe():
    # Default construction (no on_inner): INNER still stripped, no crash.
    f = ControlTagFilter()
    out = f.feed("[INNER:只在自己心里]") + f.flush()
    assert out == ""
