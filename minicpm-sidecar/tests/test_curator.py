"""Lock down the curator state machine + skill activity tracking.

These are the external-distillation Patch step (论文 3.1): skills don't
live forever — unused ones age out (active→stale→archived), pinned ones
are protected, and archive is always recoverable. If any of these break
the skill library either rots silently or loses user skills.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.evolve import (
    apply_automatic_transitions,
    is_paused,
    run_curator,
    set_paused,
    should_run_now,
)
from gateway.skills import SkillService
from gateway.skills.usage import STATE_ACTIVE, STATE_ARCHIVED, STATE_STALE


@pytest.fixture
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SkillService:
    monkeypatch.setenv("MINICPM_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("MINICPM_SKILL_DIR", str(tmp_path))
    s = SkillService()
    s.add_default_roots()
    return s


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ----------------------------------------------------------------------
# SkillUsage — activity tracking
# ----------------------------------------------------------------------

def test_bump_increments_use_count_and_reactivates_stale(svc: SkillService):
    svc.usage.bump("s")
    svc.usage.bump("s")
    rec = svc.usage.get("s")
    assert rec["use_count"] == 2
    assert rec["state"] == STATE_ACTIVE
    assert rec["last_activity_at"] is not None

    svc.usage.set_state("s", STATE_STALE)
    assert svc.usage.get("s")["state"] == STATE_STALE
    svc.usage.bump("s")  # reuse → reactivate
    assert svc.usage.get("s")["state"] == STATE_ACTIVE
    assert svc.usage.get("s")["use_count"] == 3


def test_seed_if_missing(svc: SkillService):
    assert svc.usage.seed_if_missing("fresh") is True   # new
    assert svc.usage.seed_if_missing("fresh") is False    # exists


def test_pinned_protects_state(svc: SkillService):
    svc.usage.set_pinned("p", True)
    assert svc.usage.is_pinned("p") is True
    svc.usage.set_pinned("p", False)
    assert svc.usage.is_pinned("p") is False


def test_create_skill_seeds_usage_record(svc: SkillService):
    """A freshly-created skill must have a usage record (clock anchored to now)."""
    svc.create_skill("new-skill", "desc.", "# body")
    rec = svc.usage.get("new-skill")
    assert rec["state"] == STATE_ACTIVE
    assert rec["use_count"] == 0
    assert rec["created_at"] is not None


def test_get_skill_bumps_usage(svc: SkillService):
    """Loading a skill counts as a use — drives the activity clock."""
    svc.create_skill("loaded", "desc.", "# body")
    svc.refresh()
    assert svc.usage.get("loaded")["use_count"] == 0
    svc.get_skill("loaded")
    assert svc.usage.get("loaded")["use_count"] == 1
    svc.get_skill("loaded")
    assert svc.usage.get("loaded")["use_count"] == 2


# ----------------------------------------------------------------------
# Curator state machine
# ----------------------------------------------------------------------

def test_active_skill_recently_used_stays_active(svc: SkillService):
    svc.create_skill("recent", "desc.", "# b")
    svc.usage.bump("recent")
    svc.usage._records["recent"]["last_activity_at"] = _days_ago(5)
    svc.usage._persist_locked()
    svc.refresh()
    paths = {n: s.path for n, s in svc._skills.items()}
    counts = apply_automatic_transitions(svc.usage, paths)
    assert svc.usage.get("recent")["state"] == STATE_ACTIVE
    assert counts["archived"] == 0


def test_unused_30_days_becomes_stale(svc: SkillService):
    svc.create_skill("stale-cand", "desc.", "# b")
    svc.usage.bump("stale-cand")
    svc.usage._records["stale-cand"]["last_activity_at"] = _days_ago(35)
    svc.usage._persist_locked()
    svc.refresh()
    paths = {n: s.path for n, s in svc._skills.items()}
    counts = apply_automatic_transitions(svc.usage, paths)
    assert svc.usage.get("stale-cand")["state"] == STATE_STALE
    assert counts["marked_stale"] == 1


def test_unused_90_days_archived_and_file_moved(svc: SkillService, tmp_path: Path):
    svc.create_skill("arch-cand", "desc.", "# b")
    svc.usage.bump("arch-cand")
    svc.usage._records["arch-cand"]["last_activity_at"] = _days_ago(100)
    svc.usage._persist_locked()
    svc.refresh()
    src_path = svc._skills["arch-cand"].path
    assert src_path.exists()
    paths = {n: s.path for n, s in svc._skills.items()}
    counts = apply_automatic_transitions(svc.usage, paths)
    assert svc.usage.get("arch-cand")["state"] == STATE_ARCHIVED
    assert counts["archived"] == 1
    # File moved to archive/, NOT deleted.
    assert not src_path.exists()
    archive_dir = tmp_path / "skill-archive"
    assert archive_dir.exists()
    assert any(p.name == "arch-cand.md" for p in archive_dir.iterdir())


def test_pinned_skill_never_transitions(svc: SkillService):
    svc.create_skill("pinned-old", "desc.", "# b")
    svc.usage.bump("pinned-old")
    svc.usage._records["pinned-old"]["last_activity_at"] = _days_ago(200)
    svc.usage._persist_locked()
    svc.usage.set_pinned("pinned-old", True)
    svc.refresh()
    paths = {n: s.path for n, s in svc._skills.items()}
    counts = apply_automatic_transitions(svc.usage, paths)
    # Pinned → untouched despite being ancient.
    assert svc.usage.get("pinned-old")["state"] == STATE_ACTIVE
    assert counts["archived"] == 0


def test_never_used_skill_has_grace_floor(svc: SkillService):
    """use_count==0 + younger than stale_after_days → left alone (not archived)."""
    svc.create_skill("never-used-fresh", "desc.", "# b")
    # No bump → use_count 0, created_at = now (young).
    svc.refresh()
    paths = {n: s.path for n, s in svc._skills.items()}
    counts = apply_automatic_transitions(svc.usage, paths)
    assert svc.usage.get("never-used-fresh")["state"] == STATE_ACTIVE
    assert counts["archived"] == 0


def test_stale_reused_reactivates(svc: SkillService):
    svc.create_skill("s", "desc.", "# b")
    svc.usage._records["s"]["last_activity_at"] = _days_ago(35)
    svc.usage._records["s"]["state"] = STATE_STALE
    svc.usage._persist_locked()
    svc.refresh()
    paths = {n: s.path for n, s in svc._skills.items()}
    apply_automatic_transitions(svc.usage, paths)
    assert svc.usage.get("s")["state"] == STATE_STALE
    # Now reuse it → anchor moves to now → reactivates on next pass.
    svc.usage.bump("s")
    apply_automatic_transitions(svc.usage, paths)
    assert svc.usage.get("s")["state"] == STATE_ACTIVE


# ----------------------------------------------------------------------
# Archive is recoverable
# ----------------------------------------------------------------------

def test_restore_moves_back_and_reactivates(svc: SkillService, tmp_path: Path):
    svc.create_skill("to-restore", "desc.", "# b")
    svc.usage.bump("to-restore")
    svc.usage._records["to-restore"]["last_activity_at"] = _days_ago(100)
    svc.usage._persist_locked()
    svc.refresh()
    paths = {n: s.path for n, s in svc._skills.items()}
    apply_automatic_transitions(svc.usage, paths)
    assert svc.usage.get("to-restore")["state"] == STATE_ARCHIVED

    rec = svc.usage.get("to-restore")
    archived_path = Path(rec["_archived_path"])
    assert archived_path.exists()
    dest = svc._writable_skill_root()
    ok, _ = svc.usage.restore_skill("to-restore", archived_path, dest)
    assert ok is True
    assert svc.usage.get("to-restore")["state"] == STATE_ACTIVE
    assert (dest / "to-restore.md").exists()


# ----------------------------------------------------------------------
# should_run_now — interval gating
# ----------------------------------------------------------------------

def test_should_run_now_first_call_seeds_and_defers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("MINICPM_MEMORY_DIR", str(tmp_path))
    # No prior state → first call seeds + returns False (defer one interval).
    assert should_run_now() is False
    # Second call immediately after → still within interval → False.
    assert should_run_now() is False


def test_should_run_now_true_after_interval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import gateway.evolve.curator as cur
    monkeypatch.setenv("MINICPM_MEMORY_DIR", str(tmp_path))
    # Seed state with last_run 8 days ago (> 7-day default interval).
    from gateway.evolve.curator import load_state, save_state, DEFAULT_INTERVAL_HOURS
    state = load_state()
    old = (datetime.now(timezone.utc) - timedelta(hours=DEFAULT_INTERVAL_HOURS + 1)).isoformat()
    state["last_run_at"] = old
    save_state(state)
    assert should_run_now() is True


def test_should_run_now_false_when_paused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("MINICPM_MEMORY_DIR", str(tmp_path))
    import gateway.evolve.curator as cur
    # Seed a past last_run so interval would normally fire.
    state = cur.load_state()
    state["last_run_at"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    cur.save_state(state)
    set_paused(True)
    try:
        assert should_run_now() is False  # paused short-circuits
    finally:
        set_paused(False)


# ----------------------------------------------------------------------
# run_curator persists state
# ----------------------------------------------------------------------

def test_run_curator_persists_last_run(svc: SkillService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("MINICPM_MEMORY_DIR", str(tmp_path))
    svc.create_skill("s", "desc.", "# b")
    svc.refresh()
    paths = {n: s.path for n, s in svc._skills.items()}
    summary = run_curator(svc.usage, paths)
    assert "ran_at" in summary
    assert "counts" in summary
    from gateway.evolve.curator import load_state
    state = load_state()
    assert state["last_run_at"] is not None
    assert state["last_run_summary"] == summary
