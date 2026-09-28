"""Dream cycle — 梦境固化 (autonomous memory consolidation).

潜意识子系统 (autonomous subsystem): when the pet has been idle long
enough, the gateway itself — not the user, not the renderer — runs a
consolidation pass. Faint memories are gathered; whatever provider is
available is asked to merge related fragments into distilled memories
and surface any lasting 感悟 into the core bank; the merged originals
are removed and the whole night is recorded as a dream event the pet
will remember having had.

This is the "自主记忆固化系统" from the autonomy architecture: memory
maintenance must be a structured subconscious organ, not a conscious
chore the model has to remember to do.

Design notes:
- Pure functions here (no provider calls, no server imports) so the
  prompt/apply logic is unit-testable; the orchestration lives in
  server.py's lifespan dream loop.
- Dreams are per-soul: state lives in the ACTIVE theme's directory.
- Model output reuses the <<<MEM>>> delimiters + ``parse_event_block``
  so no new parsing path exists.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..log_setup import get_logger

logger = get_logger()

# ── Tuning constants ─────────────────────────────────────────────────

# How often the dream loop wakes up to CHECK (cheap — time checks only).
CHECK_INTERVAL_SECONDS = 900  # 15 min

# The pet must have been idle this long before dreaming (a dream is a
# sleep phenomenon; it must not compete with an active conversation).
IDLE_HOURS = 6.0

# At most one dream per day.
COOLDOWN_HOURS = 24.0

# Don't bother dreaming over fewer fragments than this.
MIN_CANDIDATES = 4
# ... and never feed more than this into one dream.
MAX_CANDIDATES = 24

# Only FAINT memories are dream material (the vivid top-5 still load
# deterministically every turn — they don't need consolidating).
CANDIDATE_MAX_WEIGHT = 200

_STATE_FILE = "dream-state.json"


# ── State (per-theme) ────────────────────────────────────────────────

def load_state(memory_dir: Path) -> Dict[str, Any]:
    p = Path(memory_dir) / _STATE_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("dream state unreadable, starting fresh: %s", exc)
        return {}


def save_state(memory_dir: Path, state: Dict[str, Any]) -> None:
    p = Path(memory_dir) / _STATE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp", prefix=".dream_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, p)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Candidate selection ──────────────────────────────────────────────

def collect_candidates(store, max_candidates: int = MAX_CANDIDATES) -> List[Dict[str, Any]]:
    """Gather the faint, forgotten, unfinished fragments of memory.

    Ghost placeholders (weight below the directory floor) are PRIME
    material — the dream is where they get pieced back together.
    Core and actively-recalled (pause_decay) memories are excluded:
    a dream must never dissolve what the pet holds on purpose.
    """
    candidates = [
        e for e in store.get_all_events()
        if 0 < e.get("weight", 0) <= CANDIDATE_MAX_WEIGHT
        and not e.get("core")
        and not e.get("pause_decay")
    ]
    # Faintest first — the most-forgotten get the dream's attention.
    candidates.sort(key=lambda e: e.get("weight", 0))
    return candidates[:max_candidates]


def should_dream(
    store,
    last_conversation_at: Optional[datetime],
    state: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Time-gate the dream cycle. Returns (should_run, reason)."""
    now = now or datetime.now(timezone.utc)
    state = state or {}

    if last_conversation_at is None:
        return False, "never_talked"

    idle_hours = (now - last_conversation_at).total_seconds() / 3600.0
    if idle_hours < IDLE_HOURS:
        return False, f"idle_only_{idle_hours:.1f}h"

    last_dream = state.get("last_dream_at")
    if last_dream:
        try:
            dt = datetime.fromisoformat(str(last_dream).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (now - dt).total_seconds() < COOLDOWN_HOURS * 3600.0:
                return False, "cooldown"
        except ValueError:
            pass

    if len(collect_candidates(store)) < MIN_CANDIDATES:
        return False, "not_enough_fragments"

    return True, "due"


# ── Prompt construction ──────────────────────────────────────────────

def build_dream_prompt(
    candidates: List[Dict[str, Any]],
    mood_ctx: str = "",
) -> Tuple[str, str]:
    """Build (system, user) for the consolidation call.

    The model must output the usual <<<MEM>>> block, with a top-level
    ``dreams`` array (merged memories referencing fragment ids) instead
    of ``events``. Inventing facts is forbidden — a dream may only
    recombine what is already in the fragments.
    """
    system = (
        "你是凌凌，现在在睡觉。人在梦里会把白天零散的记忆重新整理："
        "相关的碎片会自然粘合成一段完整的记忆，有些感受会沉淀成感悟。\n"
        "规则：\n"
        "- 只允许重组下面给出的记忆碎片，绝不能编造碎片里没有的事实。\n"
        "- 相关的碎片合并成一条，content 用情绪流格式（每句带（情绪）标签），"
        "weight 100-600（梦整理出来的记忆不会比亲历的更重）。\n"
        "- 如果碎片里沉淀出长久的感悟，写进 core（写标题）。\n"
        "- 没有能合并的就少写或不写，不要硬凑。\n"
        "- dream_note 用一句话描述今晚的梦本身。\n"
        "输出格式（严格）：\n"
        "<<<MEM>>>{\"dreams\":[{\"title\":\"...\",\"content\":\"...\",\"weight\":300,"
        "\"merge_ids\":[\"evt_...\"]}],\"core\":[\"标题\"],\"dream_note\":\"...\"}<<<MEMEND>>>"
    )
    if mood_ctx:
        system += "\n\n" + mood_ctx

    lines = ["以下是你在褪色的记忆碎片（越靠前越模糊）："]
    for e in candidates:
        from .events import human_time_ago
        prefix = ""
        if e.get("emotion"):
            prefix += f"[{e['emotion']}] "
        if e.get("type") == "knowledge":
            prefix += "[知识] "
        lines.append(
            f"- id={e['id']} ({human_time_ago(e.get('created_at', ''))}) "
            f"{prefix}{e.get('title', '')}：{e.get('content', '')}"
        )
    lines.append("请整理今晚的梦。")
    return system, "\n".join(lines)


# ── Applying the dream ───────────────────────────────────────────────

def apply_dream(store, data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a parsed dream block to the event store.

    For each dream: merge the referenced fragments into one distilled
    event (weight capped at 600 — dream-born memories must not outrank
    lived experience) and remove the originals. A fragment can only be
    merged once. Then promote ``core`` picks via the shared core-bank
    logic, and record the dream itself as an event.

    Returns an outcome dict (counts + summary) for logging/state.
    """
    from .events import promote_core

    outcome: Dict[str, Any] = {
        "dreams_applied": 0,
        "fragments_merged": 0,
        "core_promoted": 0,
        "dream_event_id": None,
        "summary": "",
    }
    if not isinstance(data, dict):
        return outcome

    used_ids: set = set()
    for dream in (data.get("dreams") or []):
        if not isinstance(dream, dict):
            continue
        title = str(dream.get("title", "")).strip()
        content = str(dream.get("content", "")).strip()
        if not title or not content:
            continue
        raw_ids = [mid for mid in (dream.get("merge_ids") or []) if isinstance(mid, str)]
        merge_ids = [mid for mid in raw_ids if mid not in used_ids]
        merged = []
        for mid in merge_ids:
            evt = store.get_event(mid)
            if evt is None or evt.get("core") or evt.get("pause_decay"):
                continue
            merged.append(evt)
            used_ids.add(mid)
        if not raw_ids:
            # A pure reflection dream (the model asked for no merges) —
            # keep it, but lighter than a lived event.
            weight = max(1, min(400, int(dream.get("weight", 200) or 200)))
            evt = store.add_event(
                title=title, content=content, weight=weight,
                type_="experience", resolved=True,
            )
            outcome["dreams_applied"] += 1
            outcome["dream_event_id"] = evt["id"]
            continue
        if not merged:
            # Merge ids pointed at nothing usable (already merged,
            # missing, core) — skip rather than inventing a memory.
            continue
        weight = max(1, min(600, max(int(e.get("weight", 0)) for e in merged)))
        store.add_event(
            title=title, content=content, weight=weight,
            type_="experience", resolved=True,
        )
        for e in merged:
            store.remove_event(e["id"])
        outcome["dreams_applied"] += 1
        outcome["fragments_merged"] += len(merged)
        outcome["dream_event_id"] = title

    # Lasting 感悟 → core bank (shared cap/priority logic).
    core_names = data.get("core")
    if isinstance(core_names, list) and core_names:
        outcome["core_promoted"] = promote_core(store, core_names)

    # The dream itself becomes a memory — the pet remembers dreaming.
    note = str(data.get("dream_note", "") or "").strip()[:300]
    if outcome["dreams_applied"] or note:
        evt = store.add_event(
            title="梦",
            content=note or "昨夜整理了一些褪色的记忆（平静）。",
            weight=150,
            type_="experience", resolved=True,
        )
        outcome["dream_event_id"] = evt["id"]

    outcome["summary"] = (
        f"dreams={outcome['dreams_applied']} merged={outcome['fragments_merged']} "
        f"core={outcome['core_promoted']}"
    )
    return outcome
