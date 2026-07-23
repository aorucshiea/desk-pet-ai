"""Skill service for MiniCPM Desk Pet.

Scans SKILL.md files from configured directories, parses YAML frontmatter,
and exposes them via the gateway API. Follows opencode's SKILL.md convention
so skills authored for opencode are reusable here.

Usage:
    service = SkillService()
    service.add_skill_dir(Path("/path/to/skills"))
    await service.refresh()
    skills = service.list_skills()  # [{name, description, tags, ...}]
    skill = service.get_skill("my-skill")  # {name, description, content, metadata}
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

from .usage import SkillUsage, STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED


SKILL_MD_PATTERNS = ["SKILL.md", "*.md"]


def _find_frontmatter_colon(line: str) -> int:
    """Find the first unquoted colon in a YAML frontmatter line.

    Returns -1 if no unquoted colon found.
    """
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ":" and not in_single and not in_double:
            return i
    return -1


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a markdown file.

    Handles the subset of YAML used by Claude Code, opencode, and Hermes
    SKILL.md files:
      - Simple key: value pairs
      - Quoted string values ("..." / '...')
      - Inline arrays [item1, item2]
      - Block scalars (>, >-, |, |-) for multi-line descriptions

    Returns (metadata_dict, body_text).
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text

    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return {}, text

    front_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1:])

    metadata = {}
    i = 0
    while i < len(front_lines):
        line = front_lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        colon_pos = _find_frontmatter_colon(line)
        if colon_pos == -1:
            i += 1
            continue

        key = line[:colon_pos].strip().lower()
        value_raw = line[colon_pos + 1:].strip()

        # ── Block scalar: > or | ──
        #   description: >-
        #     Multi-line folded text
        #   description: |
        #     Multi-line literal text
        block_scalar_types = (">-", "|+", "|-", ">", "|")
        is_block = any(value_raw.startswith(t) for t in block_scalar_types)
        # Also match ">-" as ">"+"-" and "|+" as "|"+"+"
        is_block = is_block or (len(value_raw) >= 2 and value_raw[:2] in (">-", "|+", "|-"))
        is_block = is_block or (len(value_raw) >= 1 and value_raw[0] in (">", "|"))

        if is_block:
            fold = value_raw[0] == ">"  # fold (join lines with spaces) vs literal (keep newlines)
            # Consume block header line text (if any)
            block_value_start = 1
            if len(value_raw) >= 2 and value_raw[1] in "-+":
                block_value_start = 2
            header_text = value_raw[block_value_start:].strip()

            block_lines = []
            if header_text:
                block_lines.append(header_text)

            # Find the base indent of the key
            key_indent = len(line) - len(line.lstrip())
            j = i + 1
            while j < len(front_lines):
                next_raw = front_lines[j]
                if not next_raw.strip():
                    # Empty line: end of block if next non-empty is less indented
                    j += 1
                    continue
                next_indent = len(next_raw) - len(next_raw.lstrip())
                if next_indent > key_indent:
                    block_lines.append(next_raw.strip())
                    j += 1
                else:
                    break

            if fold:
                metadata[key] = " ".join(block_lines)
            else:
                metadata[key] = "\n".join(block_lines)
            i = j
            continue

        # ── Quoted string ──
        if (value_raw.startswith('"') and value_raw.endswith('"')) or \
           (value_raw.startswith("'") and value_raw.endswith("'")):
            value_raw = value_raw[1:-1]

        # ── Inline array [item1, item2] ──
        if value_raw.startswith("[") and value_raw.endswith("]"):
            inner = value_raw[1:-1]
            value_raw = [v.strip().strip("'\"") for v in inner.split(",") if v.strip()]

        metadata[key] = value_raw
        i += 1

    return metadata, body.strip()


def _default_skill_roots() -> list[Path]:
    """Default locations to scan for SKILL.md files.

    Includes paths used by opencode, Claude Code, and Hermes so existing
    skills are discovered without reconfiguration.
    """
    here = Path(__file__).resolve().parent.parent
    home = Path.home()
    roots = [
        here / "skills",
        here.parent / "skills",
        home / ".minicpm" / "skills",
        # opencode paths
        home / ".config" / "opencode" / "skills",
        # Claude Code / opencode shared paths
        home / ".claude" / "skills",
        home / ".claw" / "skills",
        # Legacy agent-compatible paths
        home / ".agents" / "skills",
        home / ".codex" / "skills",
    ]
    return roots


class SkillInfo:
    """Represents a discovered skill."""

    def __init__(self, path: Path) -> None:
        self.path: Path = path
        self.name: str = ""
        self.description: str = ""
        self.tags: list[str] = []
        self.version: str = "1.0.0"
        self.platforms: list[str] = []
        self.author: str = ""
        self.license: str = ""
        self.body: str = ""
        self._loaded: bool = False

    def load(self) -> None:
        if self._loaded:
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except Exception:
            self.name = self.path.stem
            self.body = ""
            self._loaded = True
            return

        metadata, body = _parse_frontmatter(text)
        self.name = str(metadata.get("name", self.path.stem))
        self.description = str(metadata.get("description", ""))
        self.version = str(metadata.get("version", "1.0.0"))
        self.author = str(metadata.get("author", ""))
        self.license = str(metadata.get("license", ""))
        tags_raw = metadata.get("tags", [])
        self.tags = tags_raw if isinstance(tags_raw, list) else [str(tags_raw)]
        platforms_raw = metadata.get("platforms", [])
        self.platforms = platforms_raw if isinstance(platforms_raw, list) else []
        self.body = body
        self._loaded = True

    def to_listing(self) -> dict:
        """Return a compact dict for /api/skills listing (name + description only)."""
        self.load()
        return {
            "name": self.name,
            "description": self.description[:1024] if self.description else "",
            "tags": self.tags[:8],
            "version": self.version,
        }

    def to_detail(self) -> dict:
        """Return a full dict for /api/skills/{name} detail."""
        self.load()
        return {
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "version": self.version,
            "platforms": self.platforms,
            "author": self.author,
            "license": self.license,
            "content": self.body,
            "path": str(self.path),
        }


class SkillService:
    """Discovers, indexes, and serves skills.

    Design (inspired by opencode's skill system):
      - Skills are SKILL.md files with YAML frontmatter.
      - Each skill has a unique name (derived from frontmatter or filename).
      - Discovery is additive: call add_skill_dir() for each search path.
      - refresh() rescans all registered directories.
      - list_skills() returns compact listings (name + description only,
        matching opencode's progressive disclosure pattern).
      - get_skill(name) returns full detail including body content.
    """

    def __init__(self):
        self._dirs: list[Path] = []
        self._skills: dict[str, SkillInfo] = {}
        self._refreshed = False
        # Per-skill activity tracker (use_count/state/pinned). Lives separate
        # from SKILL.md so the Markdown stays pure and auditable. The curator
        # reads this to drive active→stale→archived transitions.
        self.usage = SkillUsage()
        self.usage.load()

    def add_skill_dir(self, d: Path) -> None:
        self._dirs.append(d)

    def add_env_skill_dirs(self) -> None:
        """Add skill dirs from environment variables."""
        env = os.environ.get("MINICPM_SKILL_DIRS", "")
        if env:
            for p in env.split(os.pathsep):
                p = p.strip()
                if p:
                    self.add_skill_dir(Path(p))

    def add_default_roots(self) -> None:
        """Add default skill search paths."""
        for root in _default_skill_roots():
            if root.exists():
                self.add_skill_dir(root)
        # Also scan the writable root (where create_skill writes). Honours
        # MINICPM_SKILL_DIR if set, else ~/.minicpm/skills/. Ensures a
        # freshly-learned skill is discoverable after refresh().
        writable = self._writable_skill_root()
        if writable.exists():
            self.add_skill_dir(writable)
        # Also check for project-local .opencode/skills/ and .claude/skills/ dirs
        cwd = Path.cwd()
        for candidate in [
            cwd / ".opencode" / "skills",
            cwd / ".minicpm" / "skills",
            cwd / ".claude" / "skills",
            cwd / ".claw" / "skills",
        ]:
            if candidate.exists():
                self.add_skill_dir(candidate)

    def refresh(self) -> int:
        """Rescan all registered directories for SKILL.md files.

        Returns the number of skills found.
        """
        self._skills = {}
        seen_paths: set[Path] = set()
        for d in self._dirs:
            if not d.exists() or not d.is_dir():
                continue
            try:
                for pattern in SKILL_MD_PATTERNS:
                    for md_path in sorted(d.rglob(pattern)):
                        if md_path in seen_paths:
                            continue
                        seen_paths.add(md_path)
                        skill = SkillInfo(md_path)
                        skill.load()
                        # Deduplicate by name: later dir wins
                        self._skills[skill.name] = skill
            except PermissionError:
                continue
        self._refreshed = True
        return len(self._skills)

    def list_skills(self) -> List[dict]:
        """Return compact listings for all discovered skills.
        Caller must ensure refresh() has been called before the first query.
        """
        return [s.to_listing() for s in self._skills.values()]

    def get_skill(self, name: str) -> Optional[dict]:
        """Return full detail for a named skill, or None.
        Caller must ensure refresh() has been called before the first query.
        """
        skill = self._skills.get(name)
        if not skill:
            return None
        detail = skill.to_detail()
        # Loading a skill counts as a use — bumps use_count + last_activity_at
        # and reactivates if stale. This is the signal the curator reads.
        self.usage.bump(name)
        rec = self.usage.get(name)
        detail["state"] = rec.get("state", STATE_ACTIVE)
        detail["use_count"] = rec.get("use_count", 0)
        detail["last_activity_at"] = rec.get("last_activity_at")
        detail["pinned"] = rec.get("pinned", False)
        return detail

    def search_skills(self, query: str, max_results: int = 10) -> List[dict]:
        """Simple substring search across skill names and descriptions."""
        q = query.lower()
        results = []
        for s in self._skills.values():
            s.load()
            if q in s.name.lower() or q in s.description.lower():
                results.append(s.to_listing())
            if len(results) >= max_results:
                break
        return results

    # ------------------------------------------------------------------
    # Skill creation (the "/learn" write surface — external distillation)
    # ------------------------------------------------------------------

    @staticmethod
    def _writable_skill_root() -> Path:
        """The user-writable skill root. Defaults to ``~/.minicpm/skills/``.

        ``_default_skill_roots()`` scans this path (among others), so a
        skill written here is picked up by the next ``refresh()`` and served
        via ``/api/skills`` without restart. Override via ``MINICPM_SKILL_DIR``.
        """
        env = os.environ.get("MINICPM_SKILL_DIR", "").strip()
        if env:
            return Path(env).expanduser()
        return Path.home() / ".minicpm" / "skills"

    @staticmethod
    def _slugify(name: str) -> str:
        """Turn a skill name into a filesystem-safe slug (lowercase-hyphenated)."""
        import re
        slug = re.sub(r"[^A-Za-z0-9]+", "-", str(name).strip()).strip("-").lower()
        return slug or "untitled-skill"

    def create_skill(
        self,
        name: str,
        description: str,
        body: str,
        *,
        tags: list[str] | None = None,
        version: str = "0.1.0",
        overwrite: bool = False,
    ) -> dict:
        """Author a new SKILL.md on disk (external distillation write surface).

        The model calls this via the ``skill_create`` builtin tool (or the
        renderer's ``/learn`` flow) to turn a workflow it just executed into a
        reusable, auditable text skill — the core of the external-distillation
        framework. The skill lands in the user-writable root so it's editable
        by the user and discovered by the next ``refresh()``.

        Args:
            name: skill name (frontmatter ``name``). Slugified for the filename.
            description: ONE-sentence description (kept <=120 chars by convention;
                the system-prompt skill index may truncate past that).
            body: full Markdown body (WITHOUT frontmatter — we add it here).
            tags: optional categorization tags.
            version: semver; defaults ``0.1.0`` for a freshly-learned skill.
            overwrite: if False (default), refuse to clobber an existing skill
                of the same name. Set True to replace.

        Returns:
            ``{ok, path, name}`` on success or ``{ok:False, error}`` on refusal.
        """
        name = (name or "").strip()
        description = (description or "").strip()
        body = body or ""
        if not name:
            return {"ok": False, "error": "skill name is required"}
        if not description:
            return {"ok": False, "error": "skill description is required"}
        if not body.strip():
            return {"ok": False, "error": "skill body is required"}

        slug = self._slugify(name)
        # Frontmatter name keeps the user's casing/spacing intent; the FILENAME
        # is the slug. If two different names slugify to the same file, the
        # second write is a refusal (unless overwrite) — we never silently
        # clobber a learned skill.
        root = self._writable_skill_root()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{slug}.md"
        if path.exists() and not overwrite:
            return {"ok": False, "error": f"skill '{slug}' already exists at {path}; pass overwrite=true to replace"}

        # Build frontmatter. description with a colon gets quoted (YAML safety,
        # matching the existing SKILL.md convention).
        desc_val = description if ":" not in description else f'"{description}"'
        tag_line = ", ".join(f'"{t}"' for t in (tags or [])) or ""
        tags_yaml = f"[{tag_line}]" if tags else "[]"
        fm = (
            "---\n"
            f"name: {name}\n"
            f"description: {desc_val}\n"
            f"tags: {tags_yaml}\n"
            f"version: {version}\n"
            "---\n\n"
        )
        try:
            path.write_text(fm + body.strip() + "\n", encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": f"failed to write skill: {exc}"}

        # Pick up the new skill immediately so the next /api/skills lists it.
        try:
            self.refresh()
        except Exception:
            pass
        # Seed a usage record so the curator sees the new skill immediately
        # (clock anchored to now → grace floor protects it from early archive).
        try:
            self.usage.seed_if_missing(name)
        except Exception:
            pass
        return {"ok": True, "path": str(path), "name": name, "slug": slug}
