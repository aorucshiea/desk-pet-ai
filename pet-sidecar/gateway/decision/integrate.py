"""integrate.py - 整合层（丘脑）：多个直觉 → 一份心声

设计（2026-10-19）：
- 每个直觉按 `p_emit + boost × focus权重` 决定是否"浮现"（默认概率很低）
- 浮现的直觉分数加权平均 → 统一倾向
- **一致 → 强化置信；矛盾 → 输出"犹豫"**
- 对外只输出一份心声——LLM 的推理长度不随直觉数量增长
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional

from .registry import IntuitionRegistry


def integrate(
    registry: IntuitionRegistry,
    state: Dict[str, Any],
    needs: Dict[str, Any],
    rng: Optional[random.Random] = None,
) -> Dict[str, Any]:
    """返回 {status, thoughts, confidence, avg, parts}"""
    rng = rng or random
    parts: List[Dict[str, Any]] = []
    for name, it in registry.items().items():
        focus_boost = 1.2 if it.kind in needs.get("focus", []) else 0.25
        p = min(0.95, float(it.p_emit) + float(needs.get("boost", 0)) * focus_boost)
        if rng.random() > p:
            continue
        try:
            score = float(it.scorer(state))
        except Exception:
            continue
        score = max(0.0, min(1.0, score))
        parts.append({"name": name, "kind": it.kind, "score": round(score, 2), "weight": float(it.weight)})

    if not parts:
        return {"status": "quiet", "thoughts": [], "confidence": 0.0, "avg": 0.0, "parts": []}

    total_w = sum(p["weight"] for p in parts) or 1.0
    avg = sum(p["score"] * p["weight"] for p in parts) / total_w
    scores = [p["score"] for p in parts]
    spread = max(scores) - min(scores)
    # 置信 = 一致度 × 倾向强度
    confidence = max(0.0, min(1.0, (1.0 - spread) * (0.5 + abs(avg - 0.5))))

    thoughts: List[str] = []
    if avg >= 0.6:
        hot = [p["name"] for p in parts if p["score"] >= 0.5]
        thoughts.append("想活动一下" + (f"（{'、'.join(hot)} 都这么觉得）" if hot else ""))
    elif avg <= 0.35:
        thoughts.append("想安静待着")
    else:
        thoughts.append("有点犹豫，不知道要不要动")
    if spread >= 0.5:
        thoughts.append("心里有点矛盾（有的直觉想动，有的说别动）")

    return {
        "status": "ok",
        "thoughts": thoughts,
        "confidence": round(confidence, 2),
        "avg": round(avg, 2),
        "parts": parts,
    }
