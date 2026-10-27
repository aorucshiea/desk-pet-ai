"""Unit tests for the in-process brain stream bus (gateway/brain.py)."""

from __future__ import annotations

import asyncio
import json

import pytest

from gateway import brain


@pytest.fixture(autouse=True)
def _clean_brain():
    brain.clear()
    yield
    brain.clear()


def test_publish_assigns_sequence_and_utc_timestamp():
    first = brain.publish({"type": "round_start", "title": "轮开始", "round": "r_1"})
    second = brain.publish({"type": "speak", "title": "说出来", "text": "hi"})

    assert first["id"] == 1
    assert second["id"] == 2
    assert second["id"] == first["id"] + 1
    assert first["ts"].endswith("Z")
    assert "T" in first["ts"]
    # milliseconds precision, e.g. 2026-10-27T12:34:56.789Z
    assert len(first["ts"]) == len("2026-10-27T12:34:56.789Z")

    snapshot = brain.snapshot()
    assert [event["id"] for event in snapshot] == [1, 2]
    assert set(snapshot[0]) == {
        "id", "ts", "round", "iteration", "type", "title", "text", "meta",
    }


def test_publish_clips_text_and_meta_strings_and_is_json_safe():
    event = brain.publish({
        "type": "tool_call",
        "title": "工具 · x",
        "text": "x" * 5000,
        "meta": {
            "long": "y" * 5000,
            "nested": {"deep": "z" * 1300, "values": [object(), 1.5, None]},
            "object": object(),
        },
    })

    assert len(event["text"]) == brain.TEXT_MAX
    assert len(event["meta"]["long"]) == brain.META_STRING_MAX
    assert len(event["meta"]["nested"]["deep"]) == brain.META_STRING_MAX
    assert isinstance(event["meta"]["nested"]["values"][0], str)
    assert isinstance(event["meta"]["object"], str)
    # The whole record can always be serialized.
    json.dumps(event, ensure_ascii=False)


def test_ring_buffer_caps_at_600_and_keeps_the_newest():
    for index in range(brain.RING_MAX + 50):
        brain.publish({"type": "think", "text": str(index)})

    snapshot = brain.snapshot()
    assert len(snapshot) == brain.RING_MAX
    assert snapshot[0]["id"] == 51
    assert snapshot[-1]["id"] == brain.RING_MAX + 50


def test_subscribe_receives_live_events_and_unsubscribe_stops_them():
    async def scenario():
        queue = brain.subscribe()
        try:
            brain.publish({"type": "round_start"})
            live = await asyncio.wait_for(queue.get(), timeout=1.0)
        finally:
            brain.unsubscribe(queue)

        brain.publish({"type": "speak"})
        assert queue.empty()
        return live

    live = asyncio.run(scenario())
    assert live["type"] == "round_start"
    assert live["id"] == 1


def test_full_subscriber_queue_drops_oldest_without_raising():
    async def scenario():
        queue = brain.subscribe()
        try:
            while not queue.full():
                queue.put_nowait({"type": "filler"})
            event = brain.publish({"type": "live", "text": "still here"})
            assert queue.full()
            items = []
            while not queue.empty():
                items.append(queue.get_nowait())
            assert len(items) == queue.maxsize
            assert all(item["type"] == "filler" for item in items[:-1])
            assert items[-1]["id"] == event["id"]
            assert items[-1]["type"] == "live"
        finally:
            brain.unsubscribe(queue)

    asyncio.run(scenario())


def test_clear_resets_ring_sequence_and_subscribers():
    async def scenario():
        queue = brain.subscribe()
        brain.publish({"type": "round_start"})
        brain.clear()
        brain.publish({"type": "speak"})
        assert queue.empty()
        brain.unsubscribe(queue)

    asyncio.run(scenario())
    snapshot = brain.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0]["id"] == 1
    assert snapshot[0]["type"] == "speak"
