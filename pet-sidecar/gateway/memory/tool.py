"""``memory`` builtin tool — the model's write surface into long-term memory.

Ported from hermes-agent ``tools/memory_tool.py``'s ``memory_tool()`` dispatch
and ``MEMORY_SCHEMA``. Adapted to the desk-pet gateway's MCP ``register_builtin``
handler signature: the handler is an async callable taking ``arguments: dict``
and returning a result dict with ``content`` / ``is_error`` / ``summary``.

The store itself is owned by ``server.py`` (one per process, booted in the
lifespan) and injected here via :func:`set_memory_store`. This indirection
keeps ``tool.py`` importable without a running sidecar (the tests pass a
fresh ``MemoryStore`` directly to :func:`memory_dispatch`).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .store import MemoryStore


# Module-level singleton, set by server.py at boot. ``None`` means the
# memory subsystem hasn't been wired up yet — tool calls fail soft rather
# than crash the chat turn.
_memory_store: Optional[MemoryStore] = None


def set_memory_store(store: Optional[MemoryStore]) -> None:
    """Inject the process-wide MemoryStore (called once from server.py lifespan)."""
    global _memory_store
    _memory_store = store


def get_memory_store() -> Optional[MemoryStore]:
    return _memory_store


# ----------------------------------------------------------------------
# Error helpers — recoverable shapes that tell the model how to retry
# ----------------------------------------------------------------------

def _tool_error(message: str) -> str:
    """JSON string for a hard tool error (caller wraps into MCP result)."""
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def _missing_old_text_error(store: MemoryStore, target: str, action: str) -> str:
    """Recoverable error for replace/remove called without ``old_text``.

    replace/remove are inherently targeted — without ``old_text`` there's no
    entry to act on. A bare "old_text is required" is a dead-end for a small
    model that just omitted the field, so we return the current inventory +
    an explicit retry instruction. Mirrors the batch path's shape.
    """
    entries = store._entries_for(target)
    current = store._char_count(target)
    limit = store._char_limit(target)
    return json.dumps(
        {
            "success": False,
            "error": (
                f"'{action}' needs old_text — a short unique substring of the entry "
                f"to {action}. None was provided. Reissue the {action} with old_text "
                f"set to part of one of the current_entries below."
            ),
            "current_entries": entries,
            "usage": f"{current:,}/{limit:,}",
        },
        ensure_ascii=False,
    )


# ----------------------------------------------------------------------
# Dispatch — pure function, testable without a running sidecar
# ----------------------------------------------------------------------

def memory_dispatch(
    *,
    action: Optional[str] = None,
    target: Optional[str] = "memory",
    content: Optional[str] = None,
    old_text: Optional[str] = None,
    operations: Optional[List[Dict[str, Any]]] = None,
    store: Optional[MemoryStore] = None,
) -> Dict[str, Any]:
    """Dispatch a memory tool call to the store. Returns the store result dict.

    Two shapes:
      - Single op: ``action`` + (``content`` / ``old_text``).
      - Batch:     ``operations=[{action, content?, old_text?}, ...]`` applied
                   atomically against the final char budget in ONE call.
    """
    store = store if store is not None else _memory_store
    if store is None:
        return {"success": False, "error": "Memory is not available in this environment."}

    # Strict providers fill optional schema fields with JSON null. Treat
    # ``target: null`` as omitted so writes use the default store.
    if target is None:
        target = "memory"
    if target not in {"memory", "user"}:
        return {"success": False, "error": f"Invalid target '{target}'. Use 'memory' or 'user'."}

    # --- Batch path ----------------------------------------------------
    if operations:
        if not isinstance(operations, list):
            return {"success": False, "error": "operations must be a list of {action, content?, old_text?} objects."}
        return store.apply_batch(target, operations)

    # --- Single-op path ------------------------------------------------
    if action == "add" and not content:
        return {"success": False, "error": "Content is required for 'add' action."}
    if action == "replace" and (not old_text or not content):
        if not old_text:
            # Model omitted old_text — can't guess which entry. Return the
            # inventory + retry instruction instead of a dead-end error.
            return json.loads(_missing_old_text_error(store, target, "replace"))
        return {"success": False, "error": "content is required for 'replace' action."}
    if action == "remove" and not old_text:
        return json.loads(_missing_old_text_error(store, target, "remove"))

    if action == "add":
        return store.add(target, content or "")
    if action == "replace":
        return store.replace(target, old_text or "", content or "")
    if action == "remove":
        return store.remove(target, old_text or "")

    return {"success": False, "error": f"Unknown action '{action}'. Use: add, replace, remove"}


# ----------------------------------------------------------------------
# MCP builtin handler — adapts memory_dispatch to register_builtin's contract
# ----------------------------------------------------------------------

async def memory_tool_handler(args: dict) -> Dict[str, Any]:
    """MCP ``register_builtin`` handler. Returns ``{content, is_error, summary}``."""
    result = memory_dispatch(
        action=args.get("action"),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        operations=args.get("operations"),
    )
    is_error = not result.get("success", False)
    text = json.dumps(result, ensure_ascii=False)
    summary = result.get("error") or result.get("message") or result.get("note") or "memory updated"
    return {
        "content": [{"type": "text", "text": text}],
        "is_error": is_error,
        "summary": summary,
    }


# ----------------------------------------------------------------------
# Schema — what the model sees so it knows how to call the tool
# ----------------------------------------------------------------------

MEMORY_TOOL_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable facts to persistent memory that survive across restarts. "
        "Memory is injected into every future turn, so keep entries compact and high-signal.\n\n"
        "HOW: make ALL changes in ONE call via an 'operations' array (each item: "
        "{action, content?, old_text?}). The batch applies atomically and the char limit "
        "is checked only on the FINAL result — so one call can remove/replace stale entries "
        "to free room AND add new ones, even when an add alone would overflow. Use the bare "
        "action/content/old_text fields only for a single lone change.\n\n"
        "WHEN: save proactively when the user states a preference, correction, or personal "
        "detail, or you learn a stable fact about their environment, conventions, or workflow. "
        "Priority: user preferences & corrections > environment facts > procedures.\n\n"
        "IF FULL: an add is rejected with the current entries shown. Reissue as ONE batch "
        "that removes/shortens stale entries and adds the new one together.\n\n"
        "TARGETS: 'user' = who the user is (name, preferences, style). 'memory' = your own "
        "notes (environment, conventions, lessons).\n\n"
        "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove"],
                "description": "Single-op action. Omit when using 'operations'.",
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "'memory' for your own notes, 'user' for the user profile.",
                "default": "memory",
            },
            "content": {
                "type": "string",
                "description": "Entry content. Required for 'add' and 'replace' (single-op).",
            },
            "old_text": {
                "type": "string",
                "description": "REQUIRED for 'replace' and 'remove' (single-op): a short unique substring identifying the existing entry to modify. Omit only for 'add'.",
            },
            "operations": {
                "type": "array",
                "description": (
                    "Batch shape: a list of operations applied atomically in one call "
                    "against the final char budget. Preferred for multiple changes or "
                    "consolidation. Each item is {action, content?, old_text?}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                        "content": {"type": "string"},
                        "old_text": {"type": "string"},
                    },
                    "required": ["action"],
                },
            },
        },
        "required": ["target"],
    },
}
