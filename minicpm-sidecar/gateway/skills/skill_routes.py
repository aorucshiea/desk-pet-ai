"""FastAPI routes for the skills API.

Provides:
  GET  /api/skills        — list all discovered skills (compact)
  GET  /api/skills/{name} — get full detail for a named skill
  GET  /api/skills/search?q=... — search skills by name/description
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from . import SkillService
from .skill_models import SkillDetailResponse, SkillListResponse
from ..evolve import build_learn_prompt, run_curator, load_state, set_paused
from .usage import SkillUsage

router = APIRouter(tags=["skills"])

# Singleton skill service; lazily initialized in register_skill_routes
_service: SkillService = None  # type: ignore


def create_skill_service() -> SkillService:
    """Create and initialize the skill service with default roots."""
    svc = SkillService()
    svc.add_default_roots()
    svc.add_env_skill_dirs()
    return svc


def register_skill_routes(app, skill_service: SkillService | None = None) -> SkillService:
    """Register skill routes onto a FastAPI app.

    Args:
        app: The FastAPI application instance.
        skill_service: Optional pre-configured SkillService. If None, creates one.

    Returns:
        The SkillService instance (for potential pre-refresh at startup).
    """
    global _service
    _service = skill_service or create_skill_service()
    try:
        _service.refresh()
    except Exception:
        pass

    @app.get("/api/skills", response_model=SkillListResponse)
    async def list_skills():
        """List all discovered skills (compact: name + description only)."""
        skills = _service.list_skills()
        return {"skills": skills, "count": len(skills)}

    @app.get("/api/skills/search", response_model=SkillListResponse)
    async def search_skills(
        q: str = Query(..., min_length=1, description="Search query"),
        max_results: int = Query(10, ge=1, le=50, description="Max results"),
    ):
        """Search skills by name or description (substring match)."""
        results = _service.search_skills(q, max_results=max_results)
        return {"skills": results, "count": len(results)}

    @app.get("/api/skills/{name:path}", response_model=SkillDetailResponse)
    async def get_skill(name: str):
        """Get full detail for a named skill including full Markdown body."""
        skill = _service.get_skill(name)
        if skill is None:
            return {"skill": None, "found": False}
        return {"skill": skill, "found": True}

    @app.post("/api/skills")
    async def create_skill(payload: dict):
        """Create a new SKILL.md on disk (external-distillation write surface).

        The model calls this via the ``skill_create`` builtin tool (or the
        renderer's ``/learn`` flow) to persist a workflow as an auditable text
        skill. Body is the request envelope:
          {name, description, body, tags?, version?, overwrite?}
        """
        name = str(payload.get("name") or "").strip()
        description = str(payload.get("description") or "").strip()
        body = str(payload.get("body") or "")
        tags = payload.get("tags") if isinstance(payload.get("tags"), list) else None
        version = str(payload.get("version") or "0.1.0").strip() or "0.1.0"
        overwrite = bool(payload.get("overwrite", False))
        result = _service.create_skill(
            name, description, body,
            tags=tags, version=version, overwrite=overwrite,
        )
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    @app.post("/api/skills/learn")
    async def learn_prompt(payload: dict | None = None):
        """Return the /learn prompt for an open-ended skill-authoring request.

        The renderer's ``/learn <text>`` command calls this, gets back a prompt,
        and feeds it to the model as a normal turn. The model then gathers the
        described sources and authors a SKILL.md via ``/api/skills``.
        """
        request = ""
        if isinstance(payload, dict):
            request = str(payload.get("request") or "").strip()
        return {"prompt": build_learn_prompt(request)}

    # ── Curator (external-distillation Patch step) ──────────────────────
    # Background skill maintenance: active→stale→archived lifecycle,
    # pinned bypass, recoverable archive. Run manually here; the lifespan
    # boots an idle-triggered check (no cron daemon).
    #
    # Mounted under /api/curator/* (NOT /api/skills/curator/*) because the
    # /api/skills/{name:path} catch-all would shadow anything nested under it.

    def _live_skill_paths() -> dict:
        """Map {skill_name: on-disk SKILL.md path} for the curator's archive move."""
        out = {}
        for name, info in _service._skills.items():
            out[name] = info.path
        return out

    @app.get("/api/curator/run")
    async def curator_run():
        """Run one curator pass now. Returns the counts + run timestamp."""
        summary = run_curator(_service.usage, _live_skill_paths())
        return summary

    @app.get("/api/curator/status")
    async def curator_status():
        """Return curator run state: last_run_at, paused, last summary."""
        return load_state()

    @app.post("/api/curator/paused")
    async def curator_set_paused(payload: dict):
        """Pause/resume automatic curation (manual runs still work)."""
        paused = bool((payload or {}).get("paused", False))
        set_paused(paused)
        return {"paused": paused}

    @app.post("/api/curator/pin")
    async def curator_pin(payload: dict):
        """Pin a skill so the curator never auto-transitions it."""
        name = str((payload or {}).get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
        pinned = bool((payload or {}).get("pinned", True))
        _service.usage.set_pinned(name, pinned)
        return {"ok": True, "name": name, "pinned": pinned}

    @app.post("/api/curator/restore")
    async def curator_restore(payload: dict):
        """Restore an archived skill: move it back + reactivate."""
        name = str((payload or {}).get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
        rec = _service.usage.get(name)
        archived_path = rec.get("_archived_path")
        if not archived_path:
            return JSONResponse({"ok": False, "error": "no archived_path on record"})
        dest = _service._writable_skill_root()
        ok, msg = _service.usage.restore_skill(name, __import__("pathlib").Path(archived_path), dest)
        if not ok:
            return JSONResponse({"ok": False, "error": msg}, status_code=500)
        try:
            _service.refresh()
        except Exception:
            pass
        return {"ok": True, "name": name, "message": msg}

    return _service
