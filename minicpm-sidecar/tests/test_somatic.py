"""身体感受 (somatic sense) tests — the body reports touch to the brain.

The Electron shell reports drags and click bursts via /api/pet/interaction;
`record()` coalesces them into a fresh buffer (injected into the next
chat's system prompt) and writes throttled episodic events so meaningful
touches survive as recall-able memory.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gateway.memory import somatic
from gateway.memory.somatic import SensationBuffer, build_somatic_block, record

T0 = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)


def _ts(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_unknown_kind_is_ignored():
    buf = SensationBuffer()
    detail, title, content = record(buf, {"kind": "teleport"}, now=T0)
    assert detail is None and title is None and content is None
    assert build_somatic_block(buf, now=T0) == ""


def test_drag_phrasing_follows_distance():
    buf = SensationBuffer()
    _, near_t, near_c = record(
        buf, {"kind": "drag", "distance_px": 120, "duration_ms": 300}, now=T0)
    assert "挪了挪位置" in near_c and near_t == "被用户拎起移动"

    buf = SensationBuffer()
    _, _, mid_c = record(
        buf, {"kind": "drag", "distance_px": 900, "duration_ms": 800}, now=T0)
    assert "搬到了不远处" in mid_c

    buf = SensationBuffer()
    _, _, far_c = record(
        buf, {"kind": "drag", "distance_px": 2600, "duration_ms": 5200}, now=T0)
    assert "搬到了很远的地方" in far_c
    assert "被拎了 5 秒" in far_c


def test_click_phrasing_follows_burst_size():
    buf = SensationBuffer()
    _, _, one = record(buf, {"kind": "click", "clicks": 1}, now=T0)
    assert "点了一下" in one
    buf = SensationBuffer()
    _, _, few = record(buf, {"kind": "click", "clicks": 3}, now=T0)
    assert "点了你 3 下" in few
    buf = SensationBuffer()
    _, _, many = record(buf, {"kind": "click", "clicks": 6}, now=T0)
    assert "戳个不停" in many
    # Missing clicks defaults to a single tap.
    buf = SensationBuffer()
    _, _, defaulted = record(buf, {"kind": "click"}, now=T0)
    assert "点了一下" in defaulted


def test_event_write_is_throttled_per_kind():
    buf = SensationBuffer()
    # First gesture writes an event…
    _, title1, _ = record(buf, {"kind": "drag", "distance_px": 900}, now=T0)
    assert title1 == "被用户拎起移动"
    # …an immediate second one does NOT (cooldown)…
    _, title2, _ = record(buf, {"kind": "drag", "distance_px": 900}, now=_ts(30))
    assert title2 is None
    # …but clicks still write (independent cooldown)…
    _, title3, _ = record(buf, {"kind": "click", "clicks": 2}, now=_ts(60))
    assert title3 == "被用户点击"
    # …and after the cooldown the drag writes again.
    _, title4, _ = record(
        buf, {"kind": "drag", "distance_px": 900}, now=_ts(somatic.EVENT_WRITE_COOLDOWN_SECONDS + 5))
    assert title4 == "被用户拎起移动"


def test_recent_sensations_coalesce_within_window():
    buf = SensationBuffer()
    record(buf, {"kind": "drag", "distance_px": 100}, now=T0)
    record(buf, {"kind": "drag", "distance_px": 2000}, now=_ts(5))
    assert len(buf.fresh(_ts(6))) == 1
    # The LATEST detail wins (the coalesced entry was refreshed).
    assert "搬到了很远的地方" in buf.fresh(_ts(6))[0][1]


def test_block_uses_relative_time_and_drops_stale_sensations():
    buf = SensationBuffer()
    record(buf, {"kind": "drag", "distance_px": 900}, now=T0)
    record(buf, {"kind": "click", "clicks": 5}, now=_ts(90))

    block = build_somatic_block(buf, now=_ts(95))
    assert block.startswith("【身体感受】")
    # Drag at T0 (95s ago → 几分钟前), click at T0+90 (5s ago → 刚刚).
    assert "几分钟前" in block
    assert "刚刚" in block
    assert "戳个不停" in block

    # Past the fresh window everything drops and the block goes quiet.
    # (The click landed at +90s, so go 600s beyond THAT.)
    assert build_somatic_block(buf, now=_ts(700)) == ""


def test_block_mentions_drag_and_touch_not_mechanical():
    buf = SensationBuffer()
    record(buf, {"kind": "drag", "distance_px": 2600}, now=T0)
    block = build_somatic_block(buf, now=T0)
    assert "刚刚" in block
    assert "搬到了很远的地方" in block
    assert "不要机械复述" in block
