"""LingLing recall tool — model-initiated memory retrieval.

The model sees a faded directory of barely-remembered events in its
system prompt. When one of those fuzzy titles feels relevant to the
current conversation, the model calls recall("关键词") to retrieve
the full event.

This gives the model "I forgot something but I can try to remember"
— a fundamentally different capability from being told everything.

Registered as a builtin MCP tool in server.py.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..log_setup import get_logger
from .decay import on_event_accessed
from .events import EventStore
from . import loader

logger = get_logger()

# Module-level singleton, set by server.py at boot.
_event_store: Optional[EventStore] = None


def set_event_store(store: Optional[EventStore]) -> None:
    """Inject the process-wide EventStore (called once from server.py)."""
    global _event_store
    _event_store = store


def get_event_store() -> Optional[EventStore]:
    return _event_store


RECALL_TOOL_SCHEMA = {
    "name": "recall",
    "description": (
        "当你看到记忆目录中某个模糊的标题，觉得它和当前对话有关，"
        "但你想不起细节时，调用这个工具来回忆。输入你想回忆的关键词。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "回忆关键词 — 人名、地点、事件片段",
            },
        },
        "required": ["keyword"],
    },
}


async def recall_tool_handler(args: dict) -> Dict[str, Any]:
    """MCP register_builtin handler for the recall tool."""
    store = _event_store
    if store is None:
        return {
            "content": [{"type": "text", "text": "记忆系统不可用。"}],
            "is_error": True,
            "summary": "Event store not initialized.",
        }

    keyword = str(args.get("keyword") or "").strip()
    if not keyword:
        return {
            "content": [{"type": "text", "text": "你需要告诉我要回忆什么关键词。"}],
            "is_error": True,
            "summary": "keyword is required",
        }

    # Search for matching events
    results = store.search_by_keyword(keyword, limit=3)

    if not results:
        return {
            "content": [{"type": "text", "text": "你努力想了想，但什么都想不起来。也许这件事已经彻底忘了。"}],
            "is_error": False,
            "summary": f"No events matched '{keyword}'.",
        }

    # Take the best match
    evt = results[0]
    on_event_accessed(store, evt["id"])
    loader.add_to_session(evt["id"])

    return {
        "content": [{"type": "text", "text": f"你想起来了：[{evt['title']}] {evt['content']}"}],
        "is_error": False,
        "summary": f"Recalled: [{evt['title']}]",
    }
