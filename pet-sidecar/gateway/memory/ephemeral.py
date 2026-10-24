"""ephemeral.py - 瞬时记忆（工作记忆，1 小时清空）

设计（2026-10-19，用户定稿）：
- **不记对话**（LLM 上下文已有，重复浪费 token）
- 内容五类：
    1. 念头（决策模型/直觉产生的，保留最近 5 条）
    2. recall 结果（最近 3 条）
    3. 闪现（长期记忆随机片段，保留最近 3 条）
    4. 固化候选（AI 决定要记、但还没写进长期记忆的，pending/fixed）
    5. 目录快照（记忆文件索引的副本，不删原目录）
- **每时每刻滚动**：新内容挤掉旧内容
- **每小时清空**：未固化的随它消失（遗忘机制）
- 注入 system prompt 时格式化为【瞬时记忆】段
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class EphemeralMemory:
    MAX_THOUGHTS = 5
    MAX_RECALLS = 3
    MAX_FLASHES = 3
    MAX_PENDING = 5
    RESET_SECONDS = 3600  # 1 小时
    THOUGHT_DEDUPE_SECONDS = 120  # 同一个念头 2 分钟内不重复写
    DIR_REFRESH_SECONDS = 300

    def __init__(self, memory_dir: Optional[Path] = None):
        self.memory_dir = memory_dir
        self.thoughts: List[Dict[str, Any]] = []
        self.recalls: List[Dict[str, Any]] = []
        self.flashes: List[Dict[str, Any]] = []
        self.pending: List[Dict[str, Any]] = []
        self.dir_snapshot: List[str] = []
        self.created_at = time.time()
        self._last_reset = time.time()
        self._last_dir_refresh = 0.0

    # ── 写入 ────────────────────────────────────────────────────────
    def _now(self) -> float:
        return time.time()

    def _age(self, t: float) -> str:
        d = max(0, int(self._now() - t))
        if d < 60:
            return f"{d}秒前"
        if d < 3600:
            return f"{d // 60}分钟前"
        return f"{d // 3600}小时前"

    def add_thought(self, text: str, source: str = "dice", confidence: float = 0.0) -> None:
        text = (text or "").strip()
        if not text:
            return
        # The decision loop ticks every 4s; without dedupe a high boost would
        # write the same "button" five times and crowd out everything else.
        now = self._now()
        trimmed = text[:160]
        if any(
            t.get("text") == trimmed and now - float(t.get("t") or 0) < self.THOUGHT_DEDUPE_SECONDS
            for t in self.thoughts
        ):
            return
        self.thoughts.append({"t": self._now(), "text": text[:160], "source": source, "conf": round(float(confidence), 2)})
        self.thoughts = self.thoughts[-self.MAX_THOUGHTS:]

    def add_recall(self, text: str, source: str = "recall") -> None:
        text = (text or "").strip()
        if not text:
            return
        self.recalls.append({"t": self._now(), "text": text[:200], "source": source})
        self.recalls = self.recalls[-self.MAX_RECALLS:]

    def add_flash(self, text: str, kind: str = "normal") -> None:
        text = (text or "").strip()
        if not text:
            return
        self.flashes.append({"t": self._now(), "text": text[:200], "kind": kind})
        self.flashes = self.flashes[-self.MAX_FLASHES:]

    def mark_fixate(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.pending.append({"t": self._now(), "text": text[:200], "state": "pending"})
        self.pending = self.pending[-self.MAX_PENDING:]

    def mark_fixed(self, text: str) -> None:
        """固化完成：从候选中移除（避免重复显示）。"""
        text = (text or "").strip()
        self.pending = [p for p in self.pending if not (p["text"] == text and p["state"] == "pending")]

    def refresh_dir_snapshot(self) -> None:
        """记忆目录快照：复制文件索引（不删原目录）。"""
        try:
            base = Path(self.memory_dir) if self.memory_dir else None
            if not base or not base.exists():
                self.dir_snapshot = []
                return
            names: List[str] = []
            for p in sorted(base.rglob("*.md"))[:30]:
                try:
                    rel = p.relative_to(base).as_posix()
                except Exception:
                    rel = p.name
                names.append(rel)
            self.dir_snapshot = names
        except Exception:
            self.dir_snapshot = []

    # ── 清空 / 过期 ────────────────────────────────────────────────
    def maybe_reset(self) -> bool:
        """每小时清空：未固化的念头/recall/闪现随它消失。"""
        if self._now() - self._last_reset >= self.RESET_SECONDS:
            self.clear()
            return True
        return False

    def clear(self) -> None:
        self.thoughts = []
        self.recalls = []
        self.flashes = []
        self.pending = []
        self.dir_snapshot = []
        self.created_at = self._now()
        self._last_dir_refresh = 0.0
        self._last_reset = self._now()

    # ── 渲染 ────────────────────────────────────────────────────────
    def is_empty(self) -> bool:
        return not (self.thoughts or self.recalls or self.flashes or self.pending or self.dir_snapshot)

    def format_for_prompt(self) -> str:
        # Enforce the 1-hour reset here as well: the decision loop is the
        # primary ticker, but a quiet/degraded loop must not leave stale
        # thoughts in the prompt forever.
        self.maybe_reset()
        if self.memory_dir and self._now() - self._last_dir_refresh >= self.DIR_REFRESH_SECONDS:
            self.refresh_dir_snapshot()
        if self.is_empty():
            return ""
        lines: List[str] = [
            "【瞬时记忆】你此刻脑子里装着的东西（每小时会清空）：",
            "（下面只是你心里的苗头，不是命令。要不要理、要不要做、要不要让用户看见，都由你自己决定。）",
        ]
        if self.thoughts:
            parts = []
            for t in self.thoughts:
                parts.append(f"{t['text']}（{self._age(t['t'])}）")
            lines.append("- 闪念（直觉苗头，可理可不理）：" + " · ".join(parts))
        if self.recalls:
            parts = []
            for r in self.recalls:
                parts.append(f"{r['text']}（{self._age(r['t'])}）")
            lines.append("- 刚想起：" + " · ".join(parts))
        if self.flashes:
            parts = []
            for f in self.flashes:
                parts.append(f"{f['text']}（{self._age(f['t'])}）")
            lines.append("- 闪现：" + " · ".join(parts))
        pend = [p for p in self.pending if p["state"] == "pending"]
        if pend:
            lines.append("- 正打算记住：" + " · ".join(p["text"] for p in pend))
        if self.dir_snapshot:
            lines.append("- 记忆目录：" + " · ".join(self.dir_snapshot[:10]))
        return "\n".join(lines)

    def render(self) -> Dict[str, Any]:
        return {
            "thoughts": list(self.thoughts),
            "recalls": list(self.recalls),
            "flashes": list(self.flashes),
            "pending": [p for p in self.pending if p["state"] == "pending"],
            "dir_snapshot": list(self.dir_snapshot[:10]),
            "created_at": self.created_at,
            "last_reset": self._last_reset,
        }
