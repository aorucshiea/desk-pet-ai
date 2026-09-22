"""Splits a model stream into reasoning (<think>...</think>) and content events.

Ported from the legacy PyTorch bridge. The MiniCPM chat template wraps
reasoning in <think>...</think>; we hold any text that *might* be the start
of a tag until we know for sure, then emit either a `think` event (when
expose=True) or drop it (when expose=False). Plain text outside the tag is
always emitted as a `delta` event.

The Electron renderer (see clawd-on-desk/src/minicpm-chat.html) consumes
this exact event vocabulary.

Also hosts ControlTagFilter (B-1): strips [EMOTION:…] / [NEXT_CHAT:…]
control tags from the delta stream mid-flight so the typewriter renderer
never flashes them at the user.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable


class ThinkBlockFilter:
    OPEN_TAG = "<think>"
    CLOSE_TAG = "</think>"

    def __init__(self, *, expose: bool, start_inside: bool = False) -> None:
        self.expose = expose
        self._buf = ""
        self._mode = "inside" if start_inside else "outside"

    def feed(self, piece: str) -> list[dict]:
        self._buf += piece
        return list(self._drain())

    def flush(self) -> list[dict]:
        out = list(self._drain(final=True))
        if self._buf:
            ev = "think" if (self._mode == "inside" and self.expose) else "delta"
            if self._mode == "inside" and not self.expose:
                pass  # drop residual reasoning when caller doesn't want it
            else:
                out.append({"event": ev, "content": self._buf})
            self._buf = ""
        return out

    def _drain(self, *, final: bool = False) -> Iterable[dict]:
        while self._buf:
            if self._mode == "outside":
                idx = self._buf.find(self.OPEN_TAG)
                if idx < 0:
                    safe_len = self._safe_emit_len(self._buf, self.OPEN_TAG)
                    if safe_len <= 0:
                        if final and self._buf:
                            yield {"event": "delta", "content": self._buf}
                            self._buf = ""
                        return
                    out, self._buf = self._buf[:safe_len], self._buf[safe_len:]
                    if out:
                        yield {"event": "delta", "content": out}
                    return
                if idx > 0:
                    out, self._buf = self._buf[:idx], self._buf[idx:]
                    yield {"event": "delta", "content": out}
                self._buf = self._buf[len(self.OPEN_TAG):]
                self._mode = "inside"
            else:  # inside <think>
                idx = self._buf.find(self.CLOSE_TAG)
                if idx < 0:
                    safe_len = self._safe_emit_len(self._buf, self.CLOSE_TAG)
                    if safe_len <= 0:
                        return
                    out, self._buf = self._buf[:safe_len], self._buf[safe_len:]
                    if out and self.expose:
                        yield {"event": "think", "content": out}
                    return
                head, self._buf = self._buf[:idx], self._buf[idx + len(self.CLOSE_TAG):]
                if head and self.expose:
                    yield {"event": "think", "content": head}
                self._mode = "outside"
                # Eat exactly one trailing "\n\n" the template inserts after </think>
                if self._buf.startswith("\n\n"):
                    self._buf = self._buf[2:]
                elif self._buf.startswith("\n"):
                    self._buf = self._buf[1:]

    @staticmethod
    def _safe_emit_len(buf: str, tag: str) -> int:
        """How many chars of `buf` can be emitted without crossing a partial tag tail."""
        max_keep = len(tag) - 1
        for keep in range(min(max_keep, len(buf)), 0, -1):
            if tag.startswith(buf[-keep:]):
                return len(buf) - keep
        return len(buf)


# ── B-1: control-tag filter for streaming deltas ─────────────────────────────
# [EMOTION:happy] / [NEXT_CHAT:300] are renderer control tags the model
# appends to its reply; [WALK:dx,dy] / [WALK_DESKTOP] are BODY commands
# (身体移动 — the model walks itself). Streaming them raw made them flash
# on screen until the renderer's final sanitize pass stripped them. This
# filter removes the tags from the stream itself, holding back any
# `[`-prefix tail that might still grow into a tag (max tag length is
# short, so the hold is tiny). WALK tags are additionally surfaced to the
# caller via on_control so the stream loop can emit body-command events.

_CTRL_TAG_RE = re.compile(
    r"\[\s*(?:EMOTION|NEXT_CHAT|WALK_DESKTOP|WALK)\s*[^\]]*\]",
    re.IGNORECASE,
)
# The longest possible tag prefix we must hold before deciding it is not a
# tag: "[WALK_DESKTOP" → 13 chars. Holding 16 covers whitespace variants.
_CTRL_MAX_HOLD = 16
_CTRL_PREFIXES = ("[EMOTION:", "[NEXT_CHAT:", "[WALK:", "[WALK_DESKTOP")
_WALK_RE = re.compile(r"\[\s*WALK\s*:\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", re.IGNORECASE)


class ControlTagFilter:
    """Stateful stripper for [EMOTION:…] / [NEXT_CHAT:…] / [WALK…] tokens."""

    def __init__(self, on_control: Callable[[str, int | None, int | None], None] | None = None) -> None:
        self._buf = ""
        self._on_control = on_control

    def feed(self, piece: str) -> str:
        """Append a delta; return the safe-to-emit text with tags removed."""
        self._buf += piece
        # Surface complete WALK body-commands to the caller BEFORE they are
        # stripped — the stream loop turns them into body-command SSE events.
        if self._on_control is not None:
            for m in _WALK_RE.finditer(self._buf):
                self._on_control("walk", int(m.group(1)), int(m.group(2)))
            if re.search(r"\[\s*WALK_DESKTOP\s*\]", self._buf, re.IGNORECASE):
                self._on_control("walk_desktop", None, None)
        self._buf = _CTRL_TAG_RE.sub("", self._buf)
        # Hold back a trailing '[' fragment that may yet grow into a tag.
        # Search the WHOLE buffer (tags up to ~30 chars exceed any fixed
        # tail window when deltas split mid-tag).
        idx = self._buf.rfind("[")
        if idx >= 0:
            candidate = self._buf[idx:]
            if "]" not in candidate:
                squashed = re.sub(r"\s", "", candidate).upper()
                plausible = candidate == "[" or any(
                    # candidate still growing toward a tag, or an open tag
                    # whose closing ']' just hasn't arrived yet
                    p.startswith(squashed) or squashed.startswith(p)
                    for p in _CTRL_PREFIXES
                )
                if plausible:
                    emit, self._buf = self._buf[:idx], candidate
                    return emit
        out, self._buf = self._buf, ""
        return out

    def flush(self) -> str:
        """Stream end: emit and strip whatever remains."""
        out = _CTRL_TAG_RE.sub("", self._buf)
        self._buf = ""
        return out
