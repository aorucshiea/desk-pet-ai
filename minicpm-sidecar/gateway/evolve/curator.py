"""Curator — background skill maintenance (external distillation Patch step).

Ported from hermes-agent's ``agent/curator.py``, scaled to the desk pet:
same lifecycle state machine (active→stale→archived, pinned skip, reactivate
on reuse) and same inactivity-trigger model (no cron daemon — run when the
pet is idle and the last pass was more than ``interval_hours`` ago).

What we KEEP from upstream:
  - Inactivity-triggered, not cron: ``should_run_now()`` + idle gate.
  - ``apply_automatic_transitions()`` walks every tracked skill and moves
    active/stale/archived based on the latest activity timestamp.
  - Pinned skills bypass all transitions.
  - Archive is recoverable (move to archive/, never delete).
  - Never-used skills get a grace floor: don't archive until at least
    stale_after_days old (use=0 is absence of evidence, not staleness).

What we DROP (intentional, pet-scope simplification):
  - The forked aux-model LLM consolidation pass (Hermes' biggest complexity).
    The 0.9B pet model isn't reliable enough to author/merge skill text
    autonomously; that's a job for an API provider (P3, when one is wired).
    Pure time/usage-driven transitions are enough to keep the skill library
    from rotting.
  - cron-referenced-skill protection (the pet has no cron jobs).
  - The rich structured run-report Markdown (we return a counts dict only).

Config defaults (Hermes values, sane for a pet):
  interval_hours         = 24*7  (curator passes at most weekly)
  min_idle_hours         = 2     (only when the user's been away)
  stale_after_days      = 30     (unused → stale)
  archive_after_days    = 90     (still unused → archived, recoverable)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ..log_setup import get_logger
from ..skills.usage import (
    STATE_ACTIVE,
    STATE_ARCHIVED,
    STATE_STALE,
    SkillUsage,
)

logger = get_logger()


# ── Defaults (Hermes-aligned) ──────────────────────────────────────────────
DEFAULT_INTERVAL_HOURS = 24 * 7
DEFAULT_MIN_IDLE_HOURS = 2
DEFAULT_STALE_AFTER_DAYS = 30
DEFAULT_ARCHIVE_AFTER_DAYS = 90


# ── Curator state (last_run_at / paused) ───────────────────────────────────

def _state_file() -> Path:
    """The curator's own state file — separate from skill-usage.json.

    Holds last_run_at + paused so ``should_run_now`` can enforce the
    interval and the user can pause curation. Lives under the memory dir.
    """
    env = os.environ.get("MINICPM_MEMORY_DIR", "").strip()
    base = Path(env).expanduser() if env else (Path.home() / ".minicpm" / "memories")
    base.mkdir(parents=True, exist_ok=True)
    return base / "curator-state.json"


def _default_state() -> Dict[str, Any]:
    return {"last_run_at": None, "paused": False, "last_run_summary": None}


def load_state() -> Dict[str, Any]:
    try:
        raw = _state_file().read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return _default_state()
        # Merge with defaults so new keys appear without a migration.
        merged = _default_state()
        merged.update(data)
        return merged
    except (OSError, ValueError):
        return _default_state()


def save_state(data: Dict[str, Any]) -> None:
    try:
        _state_file().write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("curator state save failed: %s", exc)


def set_paused(paused: bool) -> None:
    state = load_state()
    state["paused"] = bool(paused)
    save_state(state)


def is_paused() -> bool:
    return bool(load_state().get("paused", False))


# ── Should we run? ──────────────────────────────────────────────────────────

def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def should_run_now(now: Optional[datetime] = None) -> bool:
    """True if enough time has passed since the last pass.

    Pinned/paused short-circuit False. On first ever run we DON'T run
    immediately — we seed state and wait a full interval, matching upstream:
    a freshly-installed pet shouldn't archive skills the user just added
    before they've had a chance to use them.
    """
    if is_paused():
        return False
    now = now or datetime.now(timezone.utc)
    state = load_state()
    last = _parse_iso(state.get("last_run_at"))
    if last is None:
        # First sight: seed + defer one interval.
        save_state({**state, "last_run_at": now.isoformat()})
        logger.info("curator: first run seeded, deferring one interval (%dh)", DEFAULT_INTERVAL_HOURS)
        return False
    return (now - last) >= timedelta(hours=DEFAULT_INTERVAL_HOURS)


# ── The transition walk ─────────────────────────────────────────────────────

def apply_automatic_transitions(
    usage: SkillUsage,
    skill_paths: Dict[str, Path],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """Walk every tracked skill and move active/stale/archived by activity.

    Pinned skills are never touched. Never-used skills (use_count==0) get a
    grace floor: don't archive one until it's at least stale_after_days old
    (young + unused may just not have hit its trigger yet). Archive moves the
    SKILL.md to ``archive/`` (recoverable), never deletes.

    Args:
        usage: the SkillUsage instance (read + mutate).
        skill_paths: {skill_name: on-disk SKILL.md path} for the live skills.
            Used to move the file on archive. Skills not in here (already
            archived, or missing from disk) only get their state flipped.
        now: override for testing.

    Returns:
        counts dict: {checked, marked_stale, archived, reactivated, seeded}.
    """
    now = now or datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=DEFAULT_STALE_AFTER_DAYS)
    archive_cutoff = now - timedelta(days=DEFAULT_ARCHIVE_AFTER_DAYS)
    counts = {"checked": 0, "marked_stale": 0, "archived": 0, "reactivated": 0, "seeded": 0}

    for name, rec in usage.all_records().items():
        counts["checked"] += 1
        if rec.get("pinned"):
            continue  # pinned bypasses everything

        # First sight with no persisted record (shouldn't normally happen
        # since bump/seed create records, but be defensive): seed + defer.
        if not rec.get("_persisted", True):
            usage.seed_if_missing(name)
            counts["seeded"] += 1
            continue

        last_activity = _parse_iso(rec.get("last_activity_at"))
        created = _parse_iso(rec.get("created_at"))
        # If never active, anchor on created_at so a fresh skill doesn't
        # archive itself immediately.
        anchor = last_activity or created or now
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)

        current = rec.get("state", STATE_ACTIVE)
        use_count = int(rec.get("use_count", 0) or 0)

        # Never-used grace floor: don't archive until at least stale_after_days old.
        never_used = use_count == 0
        if never_used and anchor > stale_cutoff:
            # Younger than the stale window + never used → leave alone.
            if current == STATE_STALE:
                usage.set_state(name, STATE_ACTIVE)
                counts["reactivated"] += 1
            continue

        if anchor <= archive_cutoff and current != STATE_ARCHIVED:
            # Move the file to archive/ (recoverable). If the skill isn't in
            # the live path map (already archived or missing), only flip state.
            src = skill_paths.get(name)
            if src is not None and Path(src).exists():
                ok, _msg = usage.archive_skill(name, Path(src))
                if ok:
                    counts["archived"] += 1
                else:
                    logger.warning("curator: archive failed for %s: %s", name, _msg)
            else:
                usage.set_state(name, STATE_ARCHIVED)
                counts["archived"] += 1
        elif anchor <= stale_cutoff and current == STATE_ACTIVE:
            usage.set_state(name, STATE_STALE)
            counts["marked_stale"] += 1
        elif anchor > stale_cutoff and current == STATE_STALE:
            usage.set_state(name, STATE_ACTIVE)
            counts["reactivated"] += 1

    return counts


def run_curator(usage: SkillUsage, skill_paths: Dict[str, Path], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """One pass: apply transitions + persist run metadata.

    Returns the summary written to state (counts + timestamp) so the caller
    can surface it via /api/skills/curator/run.
    """
    counts = apply_automatic_transitions(usage, skill_paths, now=now)
    ts = (now or datetime.now(timezone.utc)).isoformat()
    summary = {"ran_at": ts, "counts": counts}
    state = load_state()
    state["last_run_at"] = ts
    state["last_run_summary"] = summary
    save_state(state)
    return summary
