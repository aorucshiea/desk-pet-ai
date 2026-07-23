"""Mouse actions via Windows API (ctypes).

Supports left-click, right-click, double-click, and scroll wheel.
Converts OmniParser normalized bbox coordinates to pixel positions.
"""

from __future__ import annotations

import asyncio
import platform
from typing import Tuple


def get_screen_size() -> Tuple[int, int]:
    if platform.system() != "Windows":
        raise RuntimeError("screen click is only supported on Windows")
    import ctypes
    user32 = ctypes.windll.user32
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _click_at(x: int, y: int, action: str = "left_click") -> None:
    import ctypes
    import time
    user32 = ctypes.windll.user32
    user32.SetCursorPos(x, y)
    time.sleep(0.1)

    if action == "right_click":
        user32.mouse_event(0x0008, 0, 0, 0, 0)  # MOUSEEVENTF_RIGHTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0010, 0, 0, 0, 0)  # MOUSEEVENTF_RIGHTUP
    elif action == "double_click":
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    else:  # left_click (default)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP


def _scroll_at(x: int, y: int, direction: str = "down", amount: int = 3) -> None:
    import ctypes
    import time
    user32 = ctypes.windll.user32
    user32.SetCursorPos(x, y)
    time.sleep(0.1)
    # WHEEL_DELTA = 120; positive = up, negative = down
    delta = 120 * amount if direction == "up" else -120 * amount
    user32.mouse_event(0x0800, 0, 0, delta, 0)  # MOUSEEVENTF_WHEEL


async def click_element(element: dict) -> dict:
    """Perform a mouse action at the center of an OmniParser element.

    Args:
        element: A dict with ``bbox`` [x1, y1, x2, y2] (normalized 0-1),
                 optional ``action``: "left_click" (default), "right_click",
                 "double_click", or "scroll".
                 For scroll, optional ``direction`` ("up"/"down") and
                 ``amount`` (int, default 3).

    Returns:
        ``{"ok": true, "clicked": {"x": <px>, "y": <px>, ...}}``
    """
    if platform.system() != "Windows":
        raise RuntimeError("screen click is only supported on Windows")

    bbox = element.get("bbox")
    if not bbox or len(bbox) != 4:
        raise ValueError("element must have a bbox with [x1, y1, x2, y2]")

    screen_w, screen_h = get_screen_size()
    x1 = int(bbox[0] * screen_w)
    y1 = int(bbox[1] * screen_h)
    x2 = int(bbox[2] * screen_w)
    y2 = int(bbox[3] * screen_h)
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2

    action = element.get("action", "left_click")

    if action == "scroll":
        direction = element.get("direction", "down")
        amount = int(element.get("amount", 3))
        await asyncio.to_thread(_scroll_at, cx, cy, direction, amount)
        summary = f"Scrolled {direction} {amount} notches at ({cx}, {cy})"
    else:
        await asyncio.to_thread(_click_at, cx, cy, action)
        content_preview = (element.get("content") or "")[:60]
        target_desc = (
            f"'{content_preview}' ({element.get('type', 'unknown')})"
            if content_preview
            else element.get("type", "unknown")
        )
        summary = f"{action} on {target_desc} at ({cx}, {cy})"

    return {
        "ok": True,
        "clicked": {
            "x": cx,
            "y": cy,
            "action": action,
        },
        "summary": summary,
    }
