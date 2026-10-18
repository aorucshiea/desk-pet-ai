"""vision.py - 视锥（前瞻 03/05 的"看得见什么"）

桌宠只"看见"朝向前方 ±30°、400px 内的刚体。视野外的东西仍然存在于
世界几何里，但对这只猫来说是"看不见"——这比全知全能更像活物。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

FOV_HALF_DEG = 30.0
FOV_RANGE_PX = 400.0


def facing_vector(facing: Optional[str]) -> tuple[float, float]:
    return (-1.0, 0.0) if str(facing or "").lower() == "left" else (1.0, 0.0)


def bearing(dx: float, dy: float, facing: Optional[str]) -> float:
    """Signed angle to the body relative to the pet's facing, in degrees.

    Positive = to the pet's right, negative = to its left, 0 = dead ahead.
    """
    fx, fy = facing_vector(facing)
    dot = fx * dx + fy * dy
    cross = fx * dy - fy * dx
    return math.degrees(math.atan2(cross, dot))


def visible(
    dx: float,
    dy: float,
    facing: Optional[str],
    half_deg: float = FOV_HALF_DEG,
    max_dist: float = FOV_RANGE_PX,
) -> Dict[str, Any]:
    dist = math.hypot(dx, dy)
    angle = bearing(dx, dy, facing)
    return {
        "dist": round(dist, 1),
        "angle": round(angle, 1),
        "in_fov": dist <= max_dist and abs(angle) <= half_deg,
    }


def label_for_angle(angle: float) -> str:
    if abs(angle) <= 10:
        return "正前方"
    return "右前" if angle > 0 else "左前"
