"""keyword_flash.py - 关键词触发的闪现（三层闪现里最低的一层）

设计（2026-10-19）：
- 睡眠压缩产出 `keywords.json`（全局锚点，最多 24 个）。
- 用户这句话里出现锚点时，**低概率**（默认 5%）从长期记忆里勾一条
  相关记忆，写进瞬时记忆的"闪现"槽。
- 概率必须低：关键词（"项目""累"）太常见，每次都闪会显得过度联想。
- 只读，不写长期记忆；不命中就安静过去。
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any, List, Optional

DEFAULT_PROBABILITY = 0.05
MAX_WORDS = 24

_CACHE: dict = {"path": "", "mtime": 0.0, "words": []}


def load_keywords(memory_dir: Optional[Path]) -> List[str]:
    """Read `<memory_dir>/keywords.json`, cached by mtime."""
    try:
        path = Path(memory_dir) / "keywords.json" if memory_dir else None
        if path is None:
            return []
        stat = path.stat()
        key = str(path)
        if _CACHE["path"] == key and _CACHE["mtime"] == stat.st_mtime:
            return list(_CACHE["words"])
        data = json.loads(path.read_text(encoding="utf-8"))
        words = []
        for w in (data.get("keywords") or []):
            w = str(w).strip()
            if w and w not in words:
                words.append(w)
        _CACHE.update({"path": key, "mtime": stat.st_mtime, "words": words[:MAX_WORDS]})
        return list(_CACHE["words"])
    except Exception:
        return []


def maybe_flash(
    user_text: str,
    memory_dir: Optional[Path],
    event_store,
    ephemeral,
    rng: Optional[random.Random] = None,
    probability: float = DEFAULT_PROBABILITY,
) -> Optional[str]:
    """Return the flashed text if one was written to ephemeral, else None."""
    text = (user_text or "").strip()
    if not text or event_store is None or ephemeral is None:
        return None
    words = load_keywords(memory_dir)
    if not words:
        return None
    hits = [w for w in words if w in text]
    if not hits:
        return None
    if (rng or random).random() > max(0.0, min(1.0, probability)):
        return None

    for kw in hits[:3]:
        try:
            events = event_store.search_by_keyword(kw, limit=3) or []
        except Exception:
            events = []
        if not events:
            continue
        ev = (rng or random).choice(events)
        title = str(ev.get("title") or "").strip()
        content = str(ev.get("content") or "").strip()
        flash = f"被「{kw}」勾起来：{title}" + (f"——{content}" if content else "")
        flash = flash[:200]
        try:
            if any(f.get("text") == flash for f in getattr(ephemeral, "flashes", []) or []):
                return None
            ephemeral.add_flash(flash, kind="keyword")
        except Exception:
            return None
        return flash
    return None
