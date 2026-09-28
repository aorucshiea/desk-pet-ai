"""Regression tests for this round's fixes:
B-1 ControlTagFilter, B-5 token gate, B-8 directory cap, B-9 mood regression,
Holo module pure logic (scaling, prompt assembly, image trimming).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest


# ── B-1: ControlTagFilter ────────────────────────────────────────────────────

from gateway.think_filter import ControlTagFilter


class TestControlTagFilter:
    def test_strips_emotion_tag_mid_stream(self):
        f = ControlTagFilter()
        parts = ["你好", "[EMO", "TION:happy", "]今天真", "不错"]
        out = "".join(f.feed(p) for p in parts) + f.flush()
        assert "[EMOTION" not in out.upper()
        assert "你好" in out and "今天真" in out and "不错" in out

    def test_strips_next_chat_tag(self):
        f = ControlTagFilter()
        out = f.feed("结尾[NEXT_CHAT:300]") + f.flush()
        assert "[NEXT_CHAT" not in out.upper()
        assert "结尾" in out

    def test_plain_brackets_survive(self):
        f = ControlTagFilter()
        out = f.feed("查 [docs] 里的 [1] 资料") + f.flush()
        assert "[docs]" in out and "[1]" in out

    def test_lone_open_bracket_flushes_intact(self):
        f = ControlTagFilter()
        out = f.feed("unfinished [") + f.flush()
        assert out == "unfinished ["

    def test_case_insensitive(self):
        f = ControlTagFilter()
        out = f.feed("x [emotion:sad] y") + f.flush()
        assert "emotion:" not in out.lower()
        assert "x " in out and " y" in out


# ── B-9: mood regression ─────────────────────────────────────────────────────

from gateway.memory.mood import MoodStore, INDEX_REGRESSION_PER_HOUR


class TestMoodRegression:
    def _mk_store(self, tmp_path, index: float, hours_ago: float) -> MoodStore:
        s = MoodStore()
        s.load_from_disk(tmp_path)
        s._emotion_index = index
        s._index_updated_at = (
            datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        ).isoformat()
        return s

    def test_regresses_toward_zero_over_time(self, tmp_path):
        s = self._mk_store(tmp_path, -1.0, hours_ago=10)
        assert s.emotion_index == pytest.approx(
            -1.0 * (INDEX_REGRESSION_PER_HOUR ** 10), abs=1e-6
        )

    def test_instantaneous_read_is_exact(self, tmp_path):
        s = self._mk_store(tmp_path, 0.5, hours_ago=0.0001)
        assert s.emotion_index == pytest.approx(0.5, abs=1e-4)

    def test_zero_stays_zero(self, tmp_path):
        s = self._mk_store(tmp_path, 0.0, hours_ago=100)
        assert s.emotion_index == 0.0

    def test_tag_overrides_after_regression(self, tmp_path):
        s = self._mk_store(tmp_path, 1.0, hours_ago=24)
        # 24h of regression then +0.10 happy
        expected = min(1.0, 1.0 * (INDEX_REGRESSION_PER_HOUR ** 24) + 0.10)
        assert s.apply_emotion_tag("happy") == pytest.approx(expected, abs=1e-6)

    def test_roundtrip_persistence(self, tmp_path):
        s1 = self._mk_store(tmp_path, 0.7, hours_ago=0.00001)
        s1.save()
        # Reload: fresh store sees the regressed value (stored timestamp)
        s2 = MoodStore()
        s2.load_from_disk(tmp_path)
        assert s2.emotion_index == pytest.approx(s1.emotion_index, abs=1e-6)


# ── B-8: faded-directory cap ─────────────────────────────────────────────────

from gateway.memory.events import EventStore
from gateway.memory import loader as loader_mod


class TestDirectoryCap:
    def test_capped_lines(self, tmp_path):
        store = EventStore()
        store.load_from_disk(tmp_path / "events")
        # 30 title events (weight >= MIN_DIRECTORY_WEIGHT)
        for i in range(30):
            store.add_event(title=f"事件{i}", content=f"内容{i}", weight=100 + i)
        # 10 ghost events (weight < MIN_DIRECTORY_WEIGHT)
        for i in range(10):
            store.add_event(title=f"微{i}", content="几乎忘了", weight=5)
        loader_mod.reset_session()
        lines = loader_mod.build_faded_directory(store, exclude_ids=set())
        ghosts = [l for l in lines if "已模糊" in l]
        titles = [l for l in lines if "已模糊" not in l]
        assert len(ghosts) <= loader_mod.GHOST_MAX_LINES
        assert len(titles) <= loader_mod.DIRECTORY_MAX_LINES
        assert len(lines) <= loader_mod.DIRECTORY_MAX_LINES

    def test_all_reachable_events_still_listed_when_few(self, tmp_path):
        store = EventStore()
        store.load_from_disk(tmp_path / "events")
        for i in range(5):
            store.add_event(title=f"事件{i}", content=f"内容{i}", weight=100)
        loader_mod.reset_session()
        lines = loader_mod.build_faded_directory(store, exclude_ids=set())
        assert len(lines) == 5


# ── Holo module: pure logic ──────────────────────────────────────────────────

from gateway import holo_agent


class TestHoloPureLogic:
    def test_system_prompt_embeds_task_and_schema(self):
        sp = holo_agent.build_system_prompt("打开记事本")
        assert "<task>\n打开记事本\n</task>" in sp
        assert "<output_format>" in sp
        assert "click" in sp

    def test_coordinate_scaling_with_edge_clamp(self):
        # 2880x1800 desktop, target at (500,500) normalized → dead center
        assert holo_agent.scale_point(500, 500, (2880, 1800)) == (1440, 900)
        # near-corner click clamped to EDGE_MARGIN
        x, y = holo_agent.scale_point(0, 999, (2880, 1800))
        assert x == holo_agent.EDGE_MARGIN
        assert y == 1800 - holo_agent.EDGE_MARGIN

    def test_done_step_schema_roundtrip(self):
        step = holo_agent.Step(
            thought="任务完成",
            tool_call=holo_agent.DoneArgs(tool_name="done", success=True, content="ok"),
        )
        assert step.tool_call.tool_name == "done"
        assert holo_agent.render_step(step).startswith("DONE (OK)")

    def test_click_step_parse_from_json(self):
        raw = json.dumps({
            "thought": "t",
            "tool_call": {"tool_name": "click", "element": "按钮", "x": 100, "y": 200},
        }, ensure_ascii=False)
        step = holo_agent.Step.model_validate_json(raw)
        assert isinstance(step.tool_call, holo_agent.ClickArgs)
        assert "CLICK" in holo_agent.render_step(step)

    def test_trim_keeps_only_newest_n_images(self):
        def img_msg(n):
            return {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"u{n}"}}]}
        msgs = [img_msg(i) for i in range(6)]
        holo_agent.trim_to_last_n_images(msgs, n=3)
        imgs = [
            m for m in msgs
            if isinstance(m["content"], list) and m["content"][0]["type"] == "image_url"
        ]
        assert len(imgs) == 3
        # oldest one was evicted to text
        assert msgs[0]["content"][0]["type"] == "text"

    def test_runner_snapshot_default(self):
        r = holo_agent.HoloRunner()
        snap = r.snapshot()
        assert snap["status"] == "idle"
        assert snap["step_count"] == 0
