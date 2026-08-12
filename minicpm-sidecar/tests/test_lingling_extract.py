"""P0.1: event extraction chain — the <<<MEM>>> block parser.

Before this fix the model was never asked to emit event JSON, and the
legacy regex could never match real replies, so the episodic memory was
starving. These tests lock down parse_event_block (the delimited block
parser) so the fix can't silently regress.
"""

from __future__ import annotations

import pytest

from gateway.memory.events import parse_event_block


class TestParseEventBlock:
    def test_delimited_block_parses(self):
        text = (
            "今天聊了很多。\n\n"
            "<<<MEM>>>{\"events\":[{\"title\":\"用户说工作很累\","
            "\"content\":\"他提到项目赶工\",\"weight\":450}],"
            "\"mood\":{\"mood\":\"平静\",\"intensity\":40,"
            "\"reason\":\"陪聊了一阵\",\"changed\":false}}<<<MEMEND>>>"
        )
        data = parse_event_block(text)
        assert data is not None
        assert data["events"][0]["title"] == "用户说工作很累"
        assert data["events"][0]["weight"] == 450
        assert data["mood"]["mood"] == "平静"

    def test_delimited_block_with_braces_in_content(self):
        """The old regex broke on braces inside content; the delimited
        parser must not."""
        text = (
            "他说：{这两个方案都行}。\n"
            "<<<MEM>>>{\"events\":[{\"title\":\"方案讨论\","
            "\"content\":\"用户比较了{方案A}和{方案B}\",\"weight\":300}],"
            "\"mood\":{}}<<<MEMEND>>>"
        )
        data = parse_event_block(text)
        assert data is not None
        assert data["events"][0]["title"] == "方案讨论"

    def test_empty_events_block(self):
        text = "没什么值得记的。\n<<<MEM>>>{\"events\":[]}<<<MEMEND>>>"
        data = parse_event_block(text)
        assert data == {"events": []}

    def test_legacy_bare_json_fallback_empty_events(self):
        """Legacy regex only survives brace-free JSON (empty events array)."""
        text = '回复内容 {"events":[],"mood":null} 结束'
        data = parse_event_block(text)
        assert data is not None
        assert data["events"] == []

    def test_legacy_fallback_does_not_handle_event_objects(self):
        """The old regex cannot match any JSON whose events array contains
        objects (nested braces) — this is precisely why the chain was dead
        and why the delimited block is now the primary path."""
        text = '回复 {"events":[{"title":"t","content":"c","weight":1}]} 结束'
        assert parse_event_block(text) is None

    def test_no_json_returns_none(self):
        assert parse_event_block("普通回复，没有记忆块") is None
        assert parse_event_block("") is None

    def test_malformed_block_returns_none(self):
        text = "<<<MEM>>>{not json<<<MEMEND>>>"
        assert parse_event_block(text) is None
