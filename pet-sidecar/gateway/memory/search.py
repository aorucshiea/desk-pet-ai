"""Local-first search across every persistent memory surface.

No embeddings or network calls are involved.  Chinese queries are matched by
complete substrings and bigrams; English queries are matched by whole words.
Results carry enough provenance for the model (or an HTTP caller) to open the
original event, memory file, or daily conversation log.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import conversation_log
from .files import MemoryFiles

DEFAULT_LIMIT = 6
MAX_LIMIT = 50
MAX_QUERY_CHARS = 200
MAX_EVENTS = 1000
MAX_FILES = 200
MAX_FILE_CHARS = 16_000
MAX_LOG_DAYS = 90
MAX_LOG_FILE_BYTES = 1_000_000
# A search is interactive: never let one query read the entire 90-day archive.
# Newest days are read first and the scan stops when this budget is spent.
MAX_LOG_TOTAL_BYTES = 8_000_000
MAX_LOG_TURNS = 500
MAX_LOG_TURNS_TOTAL = 5_000
SNIPPET_CHARS = 180

_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_WORD_RE = re.compile(r"[a-z0-9_]+")
_SCOPES = {
    "all": ("events", "files", "logs"),
    "events": ("events",),
    "event": ("events",),
    "files": ("files",),
    "file": ("files",),
    "memory": ("files",),
    "logs": ("logs",),
    "log": ("logs",),
    "conversations": ("logs",),
    "conversation": ("logs",),
}


def _norm(text: Any) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def _query_parts(query: str) -> tuple[List[str], List[str], List[str]]:
    """Return complete Chinese runs, Chinese bigrams, and English words."""
    normalized = _norm(query).strip()
    runs = _CJK_RUN_RE.findall(normalized)
    grams: set[str] = set()
    for run in runs:
        if len(run) == 1:
            grams.add(run)
        else:
            grams.update(run[i:i + 2] for i in range(len(run) - 1))
    words = list(dict.fromkeys(_WORD_RE.findall(normalized)))
    return runs, sorted(grams), words


def lexical_score(query: str, text: str) -> float:
    """Small deterministic lexical score; zero means no query evidence."""
    q = _norm(query).strip()
    value = _norm(text)
    if not q or not value:
        return 0.0
    runs, grams, words = _query_parts(q)
    score = 0.0
    # Full-substring matching is for Chinese phrases. English must match
    # whole words below ("coff" must not retrieve "coffee").
    if runs and q in value:
        score += 4.0
    for run in runs:
        if len(run) > 1 and run in value:
            score += 2.0
    if grams:
        hits = sum(1 for gram in grams if gram in value)
        score += 3.0 * hits / len(grams)
    if words:
        text_words = set(_WORD_RE.findall(value))
        hits = sum(1 for word in words if word in text_words)
        score += 3.0 * hits / len(words)
    return score


def _combined_score(query: str, title: str, content: str) -> float:
    title_score = lexical_score(query, title)
    content_score = lexical_score(query, content)
    combined = lexical_score(query, f"{title}\n{content}")
    return max(combined, title_score * 1.25, content_score)


def _snippet(query: str, text: str, title: str = "") -> str:
    source = str(text or title or "").strip()
    if len(source) <= SNIPPET_CHARS:
        return source
    _, grams, words = _query_parts(query)
    needles = [str(query or "").strip(), *grams, *words]
    lower = source.casefold()
    positions = [(lower.find(_norm(needle)), needle) for needle in needles if needle]
    positions = [(p, n) for p, n in positions if p >= 0]
    pos = min(positions, key=lambda item: (item[0], -len(item[1])))[0] if positions else 0
    start = max(0, pos - SNIPPET_CHARS // 3)
    end = min(len(source), start + SNIPPET_CHARS)
    start = max(0, end - SNIPPET_CHARS)
    return ("…" if start else "") + source[start:end].strip() + ("…" if end < len(source) else "")


def _clamp_limit(limit: Any) -> int:
    try:
        return max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _selected_scopes(scope: Any) -> tuple[str, ...]:
    return _SCOPES.get(str(scope or "all").strip().lower(), _SCOPES["all"])


def _event_recency(created_at: Any, now: datetime) -> float:
    try:
        created = datetime.fromisoformat(str(created_at or "").replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - created.astimezone(timezone.utc)).total_seconds() / 86400.0)
    except (TypeError, ValueError):
        age_days = 365.0
    return 0.75 + 0.25 * math.exp(-age_days / 30.0)


def _event_results(store: Any, query: str, now: datetime) -> List[Dict[str, Any]]:
    if store is None:
        return []
    results = []
    try:
        events = list(store.get_all_events())[:MAX_EVENTS]
    except Exception:
        return []
    for event in events:
        title = str(event.get("title") or "")
        content = str(event.get("content") or "")
        score = _combined_score(query, title, content)
        if score <= 0:
            continue
        try:
            weight = max(0.0, min(999.0, float(event.get("weight", 100))))
        except (TypeError, ValueError):
            weight = 100.0
        score *= (0.5 + weight / 1000.0) * _event_recency(event.get("created_at"), now)
        results.append({
            "source": "event",
            "event_id": event.get("id", ""),
            "title": title,
            "snippet": _snippet(query, content, title),
            "score": score,
            "weight": weight,
            "created_at": event.get("created_at", ""),
        })
    return results


def _file_results(memory_dir: Path, query: str) -> List[Dict[str, Any]]:
    files = MemoryFiles(memory_dir)
    results = []
    try:
        paths = files.tree()[:MAX_FILES]
    except Exception:
        return []
    for path in paths:
        # Read through MemoryFiles rather than opening a model-supplied path.
        item = files.read(path)
        if not item.get("success"):
            continue
        content = str(item.get("content") or "")[:MAX_FILE_CHARS]
        score = _combined_score(query, path, content)
        if score <= 0:
            continue
        results.append({
            "source": "memory_file",
            "path": path,
            "title": path.rsplit("/", 1)[-1],
            "snippet": _snippet(query, content, path),
            "score": score,
        })
    return results


def _read_log_bounded(memory_dir: Path, day: str, max_bytes: Optional[int] = None) -> tuple[List[dict], int]:
    """Read one day's log up to a byte cap; return ``(turns, bytes_read)``.

    The byte count lets :func:`_log_results` enforce a total budget across
    days instead of silently reading 90 MB of JSONL on every search.
    """
    if not conversation_log.valid_day(day):
        return [], 0
    limit = MAX_LOG_FILE_BYTES if max_bytes is None else max(0, min(MAX_LOG_FILE_BYTES, int(max_bytes)))
    if limit <= 0:
        return [], 0
    path = Path(memory_dir) / "conversations" / f"{day}.jsonl"
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit)
        turns = []
        for line in raw.decode("utf-8", "ignore").splitlines()[:MAX_LOG_TURNS]:
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                turns.append(value)
        return turns, len(raw)
    except OSError:
        return [], 0


def _log_results(memory_dir: Path, query: str) -> List[Dict[str, Any]]:
    results = []
    remaining_bytes = MAX_LOG_TOTAL_BYTES
    remaining_turns = MAX_LOG_TURNS_TOTAL
    # Newest first: if the budget runs out, recent memories win over the
    # archive. Ranking later still sorts by relevance.
    days = conversation_log.available_days(memory_dir)[-MAX_LOG_DAYS:]
    for day in reversed(days):
        if remaining_bytes <= 0 or remaining_turns <= 0:
            break
        turns, used = _read_log_bounded(memory_dir, day, max_bytes=remaining_bytes)
        remaining_bytes -= used
        for index, turn in enumerate(turns):
            if remaining_turns <= 0:
                break
            remaining_turns -= 1
            role = str(turn.get("role") or "")
            content = str(turn.get("content") or "")[:conversation_log.MAX_CHARS]
            score = lexical_score(query, content)
            if score <= 0:
                continue
            results.append({
                "source": "conversation",
                "day": day,
                "turn": index,
                "role": role,
                "snippet": _snippet(query, content),
                "score": score,
            })
    return results


def search_memory(
    query: str,
    event_store: Any = None,
    memory_dir: Optional[Path] = None,
    *,
    limit: int = DEFAULT_LIMIT,
    scope: str = "all",
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Search selected memory sources and return a ranked, bounded list."""
    query = str(query or "").strip()[:MAX_QUERY_CHARS]
    if not query:
        return []
    root = Path(memory_dir) if memory_dir is not None else conversation_log.default_memory_dir()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    selected = _selected_scopes(scope)
    results: List[Dict[str, Any]] = []
    if "events" in selected:
        results.extend(_event_results(event_store, query, current))
    if "files" in selected:
        results.extend(_file_results(root, query))
    if "logs" in selected:
        results.extend(_log_results(root, query))
    results.sort(key=lambda item: (-item["score"], item["source"], str(item.get("event_id") or item.get("path") or item.get("day") or ""), item.get("turn", -1)))
    bounded = results[:_clamp_limit(limit)]
    for item in bounded:
        item["score"] = round(float(item["score"]), 4)
    return bounded


def search(
    query: str,
    event_store: Any = None,
    memory_dir: Optional[Path] = None,
    *,
    limit: int = DEFAULT_LIMIT,
    scope: str = "all",
) -> Dict[str, Any]:
    """Structured API/tool response wrapper around :func:`search_memory`."""
    results = search_memory(
        query, event_store, memory_dir, limit=limit, scope=scope,
    )
    return {
        "ok": True,
        "query": str(query or "").strip()[:MAX_QUERY_CHARS],
        "scope": str(scope or "all"),
        "count": len(results),
        "results": results,
    }


async def memory_search_tool_handler(
    args: dict,
    *,
    event_store: Any = None,
    memory_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Adapt local search to the builtin MCP handler contract."""
    try:
        response = search(
            str((args or {}).get("query") or ""),
            event_store,
            memory_dir,
            limit=(args or {}).get("limit", DEFAULT_LIMIT),
            scope=str((args or {}).get("scope") or "all"),
        )
        if not response["query"]:
            response["ok"] = False
            response["error"] = "query is required"
        text = json.dumps(response, ensure_ascii=False)
        return {
            "content": [{"type": "text", "text": text}],
            "is_error": not response["ok"],
            "summary": f"memory_search: {response['count']} result(s)",
            **response,
        }
    except Exception as exc:
        return {
            "content": [{"type": "text", "text": f"记忆检索失败：{exc}"}],
            "is_error": True,
            "summary": "memory_search failed",
            "ok": False,
            "results": [],
        }


MEMORY_SEARCH_TOOL_SCHEMA = {
    "name": "memory_search",
    "description": (
        "在本机检索你的三类记忆：事件、模型自己的「记忆」文件夹、每日对话日志。"
        "适合按具体内容找记忆，不需要联网。scope 可选 all/events/files/logs。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要找的词、短语或事件片段"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
            "scope": {"type": "string", "enum": ["all", "events", "files", "logs"], "default": "all"},
        },
        "required": ["query"],
    },
}
