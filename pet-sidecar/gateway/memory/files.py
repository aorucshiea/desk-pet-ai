"""MemoryFiles — the model-owned 「记忆」 folder (free-form memory tree).

Replaces the fixed MEMORY.md / USER.md pair: the model decides its own
memory taxonomy. It creates subfolders (经验 / 经历 / 技能 / 知识 /
whatever it invents) and writes .md files inside them, or keeps a single
「记忆.md」 at the root as its main memory file (injected whole into the
system prompt every turn).

The gateway enforces only the sandbox: everything stays inside the 记忆
folder, UTF-8, .md extension only, bounded file size and file count, no
traversal. Structure is the model's business — self-organization is the
point (机长 2026-10-02：这俩专门的类别记忆根本不需要存在).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..log_setup import get_logger

logger = get_logger()

ROOT_DIR_NAME = "记忆"
MAIN_FILE_NAME = "记忆.md"

MAX_FILE_CHARS = 32_000   # per-file write cap (a .md, not a dump)
MAX_FILES = 200           # total .md files in the tree
MAX_READ_CHARS = 16_000   # read truncation guard
MAX_PATH_LEN = 200

# Path segments: no separators (already split), no control chars, no
# Windows-forbidden characters, no leading dots (hidden files).
_SEG_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


class MemoryFiles:
    """Sandboxed .md file tree rooted at <memory_dir>/记忆."""

    def __init__(self, memory_dir: Path | str) -> None:
        self.root = Path(memory_dir) / ROOT_DIR_NAME
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("could not create 记忆 folder %s: %s", self.root, exc)

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def sanitize(self, rel: Optional[str]) -> Optional[str]:
        """Normalize a model-supplied relative path into a safe posix
        subpath, or None when it escapes the sandbox / is not .md."""
        if not rel or not isinstance(rel, str):
            return None
        rel = rel.strip().replace("\\", "/").lstrip("/")
        if ":" in rel:
            return None
        # Any ".." segment is rejected outright — never silently reinterpreted
        # (a/../../b.md must not quietly become a/b.md).
        raw_parts = rel.split("/")
        if any(p == ".." for p in raw_parts):
            return None
        parts = [p.strip() for p in raw_parts if p.strip() not in ("", ".")]
        if not parts:
            return None
        for p in parts:
            if _SEG_RE.search(p) or p.startswith("."):
                return None
        cleaned = "/".join(parts)
        if len(cleaned) > MAX_PATH_LEN or not cleaned.lower().endswith(".md"):
            return None
        return cleaned

    def _resolve(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            return None  # belt and braces — sanitize already forbids ..!
        return path

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def write(self, rel: str, content: str) -> Dict[str, Any]:
        path_rel = self.sanitize(rel)
        if not path_rel:
            return {"success": False, "error": "invalid path (use a relative .md path like 记忆.md or 经历/xxx.md)"}
        if not isinstance(content, str) or not content.strip():
            return {"success": False, "error": "content is required for write"}
        if len(content) > MAX_FILE_CHARS:
            return {
                "success": False,
                "error": f"content too large ({len(content)} chars); cap is {MAX_FILE_CHARS}. Split the file or shorten it.",
            }
        path = self._resolve(path_rel)
        if path is None:
            return {"success": False, "error": "invalid path"}
        if not path.exists() and self.count() >= MAX_FILES:
            return {
                "success": False,
                "error": f"memory tree is full ({MAX_FILES} files). Consolidate: merge or delete files before adding new ones.",
            }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existed = path.exists()
            path.write_text(content, encoding="utf-8")
            return {
                "success": True,
                "path": path_rel,
                "chars": len(content),
                "note": "updated" if existed else "created",
            }
        except Exception as exc:
            return {"success": False, "error": f"write failed: {exc}"}

    def read(self, rel: str) -> Dict[str, Any]:
        path_rel = self.sanitize(rel)
        if not path_rel:
            return {"success": False, "error": "invalid path"}
        path = self._resolve(path_rel)
        if path is None or not path.is_file():
            return {"success": False, "error": f"no such memory file: {path_rel}"}
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            return {"success": False, "error": f"read failed: {exc}"}
        if len(content) > MAX_READ_CHARS:
            content = content[:MAX_READ_CHARS] + f"\n\n…（截断：文件共 {len(content)} 字符，read 只返回前 {MAX_READ_CHARS}）"
        return {"success": True, "path": path_rel, "content": content}

    def delete(self, rel: str) -> Dict[str, Any]:
        path_rel = self.sanitize(rel)
        if not path_rel:
            return {"success": False, "error": "invalid path"}
        path = self._resolve(path_rel)
        if path is None or not path.is_file():
            return {"success": False, "error": f"no such memory file: {path_rel}"}
        try:
            path.unlink()
            return {"success": True, "path": path_rel, "note": "deleted"}
        except Exception as exc:
            return {"success": False, "error": f"delete failed: {exc}"}

    def count(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(1 for p in self.root.rglob("*.md") if p.is_file())

    def tree(self) -> List[str]:
        """Sorted flat list of relative posix paths (the prompt shows this)."""
        if not self.root.is_dir():
            return []
        out: List[str] = []
        for p in sorted(self.root.rglob("*.md")):
            if p.is_file():
                rel = p.relative_to(self.root).as_posix()
                out.append(rel)
        return out

    def read_main(self) -> str:
        """Content of 记忆.md (the main memory file), or '' — injected into
        the system prompt every turn."""
        path = self.root / MAIN_FILE_NAME
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")[:MAX_READ_CHARS]
        except Exception as exc:
            logger.warning("read 记忆.md failed: %s", exc)
        return ""


# Module-level singleton, wired by memory_context (per active theme).
_memory_files: Optional[MemoryFiles] = None


def set_memory_files(files: Optional[MemoryFiles]) -> None:
    global _memory_files
    _memory_files = files


def get_memory_files() -> Optional[MemoryFiles]:
    return _memory_files
