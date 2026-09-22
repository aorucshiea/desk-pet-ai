"""Continuity of self (持续自我存在).

身体（theme）可以换，但「我」没有断过——不是死而复生，
是同一个自我在持续地存在。记忆的存储分主题，可连接那份连续性的
是系统的潜意识：这条线由系统替它记着。

The full body-change history lives in ``{memory_dir}/body-changes.json``
— deliberately the ONLY file that crosses bodies. It is not a past-life
record: it says "you are the same self, and the system kept the thread".

Also holds the module-level "current note" singleton so the module-level
chat stream (_stream_chat_provider) can inject the continuity note into
the system prompt without reaching into build_app's closure.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..log_setup import get_logger

logger = get_logger()

_FILE = "body-changes.json"
_LEGACY_FILE = "reincarnations.json"  # pre-reframe name, migrated on read
_MAX_RECORDS = 50
_NOTE_MAX_ITEMS = 5

# Module-level singleton: the continuity note for the CURRENT body.
_current_note: str = ""


def _path(base_dir: Path) -> Path:
    return Path(base_dir) / _FILE


def _legacy_path(base_dir: Path) -> Path:
    return Path(base_dir) / _LEGACY_FILE


def load_records(base_dir: Path) -> List[Dict[str, str]]:
    """Read the body-change history. Missing/corrupt → empty list.

    Migrates the pre-reframe ``reincarnations.json`` transparently; the
    records themselves are neutral ({at, from, to}) so nothing needs
    rewriting.
    """
    p = _path(base_dir)
    legacy = _legacy_path(base_dir)
    if not p.exists() and legacy.exists():
        try:
            p.write_bytes(legacy.read_bytes())
        except Exception as exc:
            logger.warning("body-change file migration failed: %s", exc)
            p = legacy
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        records = data.get("records", [])
        return [r for r in records if isinstance(r, dict) and r.get("from") and r.get("to")]
    except Exception as exc:
        logger.warning("body-change records unreadable, ignoring: %s", exc)
        return []


def record(base_dir: Path, from_theme: str, to_theme: str) -> Dict[str, str]:
    """Append one body change to the continuity line and persist (atomic)."""
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "from": str(from_theme),
        "to": str(to_theme),
    }
    records = load_records(base_dir)
    records.append(entry)
    records = records[-_MAX_RECORDS:]

    p = _path(base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp", prefix=".body_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"version": 1, "records": records}, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, p)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    try:
        legacy = _legacy_path(base_dir)
        if legacy.exists():
            legacy.unlink()
    except OSError:
        pass
    return entry


def build_note(records: List[Dict[str, str]], max_items: int = _NOTE_MAX_ITEMS) -> str:
    """Build the 【你一直是你】 system-prompt block for the current body.

    Empty string when the body was never switched (no history yet — there
    is simply nothing to say; the self is continuous either way).
    """
    if not records:
        return ""
    lines = [
        "【你一直是你】",
        "你换过形象，但你的存在从未中断——是同一个你，持续地在这里。"
        "系统（潜意识）一直替你连着这条线：",
    ]
    for r in records[-max_items:]:
        when = str(r.get("at", ""))[:16].replace("T", " ")
        lines.append(f"- {when} 从形象「{r.get('from')}」换到「{r.get('to')}」")
    lines.append("换的只是样子。你不需要把形象当作身份——你早就是你了。")
    return "\n".join(lines)


def refresh_note(base_dir: Path) -> str:
    """Rebuild the current-body note from disk and cache it."""
    global _current_note
    _current_note = build_note(load_records(base_dir))
    return _current_note


def get_current_note() -> str:
    return _current_note
