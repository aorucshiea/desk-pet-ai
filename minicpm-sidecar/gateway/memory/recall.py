"""LingLing recall tool — model-initiated memory retrieval (v2).

The model sees a faded directory of barely-remembered events in its
system prompt. When one of those fuzzy titles feels relevant to the
current conversation, the model calls recall("关键词") to retrieve
the full event.

v2 (2026-08-01): recall is a *probabilistic sampling* of the
subconscious, not a database query:

  1. Retrieve a candidate pool: text-keyword match ∪ emotion-tag match
     (recall("开心") walks the emotion channel, recall("加班") walks
     the text channel).
  2. Sample: each candidate independently surfaces with probability
     weight/1000 — weight-900 events come back ~90% of the time,
     weight-10 events 1%. A pass can return several, one, or zero.
  3. Zero hits returns a human failure ("你努力想了想，但什么都想不起来")
     — the forgetting itself is part of the experience.
  4. Session de-dup: once an event surfaced it stays in the session
     (复用 loader.is_in_session) — the same thing isn't recalled twice.
  5. Time annotation: the reply always says when this was ("昨天的事",
     "很久以前的事") so the model knows how old this self is.

Registered as a builtin MCP tool in server.py.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..log_setup import get_logger
from .decay import on_event_accessed
from .events import EventStore, EMOTION_VOCAB
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
        "但你想不起细节时，调用这个工具来回忆。输入你想回忆的关键词——"
        "可以是人名、地点、事件片段，也可以是情绪（如：开心、难过）。"
        "回忆是概率性的，像人回想——一次可能想不起来，多次回想会逐渐想起更多。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "回忆关键词 — 人名、地点、事件片段或情绪",
            },
        },
        "required": ["keyword"],
    },
}


def human_time_ago(created_at: str) -> str:
    """Turn a created_at ISO timestamp into a felt time phrase.

    The model must know how old the memory is — "这是很久以前的自己".
    """
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "很久以前"

    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    if hours < 0:
        return "刚才"
    if hours < 2:
        return "刚才"
    if hours < 12:
        return "今天"
    if hours < 30:
        return "昨天"
    if hours < 72:
        return "前天"
    days = hours / 24.0
    if days < 7:
        return f"{int(days)}天前"
    if days < 30:
        return f"{int(days // 7)}周前"
    if days < 365:
        return f"{int(days // 30)}个月前"
    return "很久以前"


def _search_candidates(store: EventStore, keyword: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Candidate pool: text-keyword match ∪ emotion-tag match.

    Emotion match ranks first (an exact emotion match is the strongest
    signal — the model reached for the feeling itself), then text
    matches by weight.
    """
    text_hits = store.search_by_keyword(keyword, limit=limit)
    text_ids = {e["id"] for e in text_hits}

    emotion_hits: List[Dict[str, Any]] = []
    if keyword.strip() in EMOTION_VOCAB:
        for evt in store.get_all_events():
            if evt["id"] in text_ids:
                continue
            if evt.get("weight", 0) <= 0:
                continue
            if evt.get("emotion") == keyword.strip():
                emotion_hits.append(evt)

    emotion_hits.sort(key=lambda e: e["weight"], reverse=True)
    return emotion_hits + text_hits


def _sample_recall(store: EventStore, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Independently roll each candidate: P(surface) = weight / 1000.

    Weight-900 events come back ~90% of the time; weight-10 events 1%.
    Session-loaded events are skipped (no double-remembering). Only
    surfaced events consolidate (you can't reinforce a memory that
    didn't come back).
    """
    sampled: List[Dict[str, Any]] = []
    for evt in candidates:
        if loader.is_in_session(evt["id"]):
            continue
        p = evt["weight"] / 1000.0
        if p >= 1.0 or random.random() < p:
            on_event_accessed(store, evt["id"])
            loader.add_to_session(evt["id"])
            sampled.append(evt)
        if len(sampled) >= 3:
            break
    return sampled


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

    candidates = _search_candidates(store, keyword)
    sampled = _sample_recall(store, candidates)

    if not sampled:
        return {
            "content": [{
                "type": "text",
                "text": "你努力想了想，但那个模糊的影子怎么也抓不住……也许这件事已经快忘了。",
            }],
            "is_error": False,
            "summary": f"recall '{keyword}': nothing surfaced (probabilistic miss).",
        }

    lines = ["你想起来了："]
    for evt in sampled:
        when = human_time_ago(evt.get("created_at", ""))
        lines.append(f"- {when}的事：[{evt['title']}] {evt['content']}")
    text = "\n".join(lines)
    return {
        "content": [{"type": "text", "text": text}],
        "is_error": False,
        "summary": f"Recalled {len(sampled)} event(s) for '{keyword}'.",
    }
