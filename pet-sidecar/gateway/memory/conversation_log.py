"""conversation_log.py - 每日对话日志（睡眠压缩的原始输入）

每条用户消息 / 助手回复追加到 `memories/conversations/YYYY-MM-DD.jsonl`。
睡眠压缩（sleep_compress）读它 → 产出带标题的记忆条 → 写入潜意识记忆。
压缩完成后原日志可以删除（保留期由调用方决定）。

设计（2026-10-19）：
- 只存原始逐字内容，不存 system / 工具调用 / 思考块
- 单条截断到 4000 字，防止异常长消息撑爆磁盘
- 失败静默（日志不该影响对话主链路）
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

MAX_CHARS = 4000
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def default_memory_dir() -> Path:
    """Platform-aware memory root.

    The Electron host always injects PET_MEMORY_DIR; this fallback is for
    direct CLI runs. It must NOT hardcode ``%APPDATA%``: os.path.expandvars
    does not expand ``%VAR%`` on POSIX, which used to create a literal
    ``%APPDATA%`` directory on macOS/Linux.
    """
    env = os.environ.get("PET_MEMORY_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "deskpt" / "memories"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "deskpt" / "memories"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "deskpt" / "memories"


def valid_day(day: str) -> bool:
    """Only accept YYYY-MM-DD before building a path from it."""
    return bool(day) and bool(_DAY_RE.match(day))


def _conversation_dir(memory_dir: Optional[Path] = None) -> Path:
    base = Path(memory_dir) if memory_dir else default_memory_dir()
    return base / "conversations"


def append_turn(role: str, content: str, memory_dir: Optional[Path] = None) -> bool:
    """追加一条对话到当天日志。返回是否写入成功。"""
    if not content or not content.strip():
        return False
    try:
        d = _conversation_dir(memory_dir)
        d.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        path = d / f"{day}.jsonl"
        rec = {
            "t": datetime.now().isoformat(timespec="seconds"),
            "role": role,
            "content": content.strip()[:MAX_CHARS],
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def read_day(day: str, memory_dir: Optional[Path] = None) -> List[dict]:
    """读取某一天（YYYY-MM-DD）的对话记录。

    ``day`` may come from an HTTP body (/api/compress), so reject anything
    that is not a literal date before joining it into a path.
    """
    if not valid_day(day):
        return []
    try:
        path = _conversation_dir(memory_dir) / f"{day}.jsonl"
        if not path.exists():
            return []
        out: List[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out
    except Exception:
        return []


def format_day(day: str, memory_dir: Optional[Path] = None) -> str:
    """把一天的对话格式化成压缩模型的输入文本。"""
    turns = read_day(day, memory_dir)
    if not turns:
        return ""
    lines = []
    for t in turns:
        who = "用户" if t.get("role") == "user" else "你"
        lines.append(f"{who}：{t.get('content', '')}")
    return "\n".join(lines)


def available_days(memory_dir: Optional[Path] = None) -> List[str]:
    """列出有日志的日期（升序）。"""
    try:
        d = _conversation_dir(memory_dir)
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.jsonl"))
    except Exception:
        return []


def prune(days_to_keep: int = 7, memory_dir: Optional[Path] = None) -> int:
    """删除超过保留期的日志文件，返回删除数量。"""
    try:
        d = _conversation_dir(memory_dir)
        if not d.exists():
            return 0
        files = sorted(d.glob("*.jsonl"))
        stale = files[:-days_to_keep] if len(files) > days_to_keep else []
        n = 0
        for p in stale:
            try:
                p.unlink()
                n += 1
            except Exception:
                pass
        return n
    except Exception:
        return 0
