"""integrate.py - 整合层（丘脑）：多个直觉 → 一份「心声」

设计（2026-10-19，用户定稿第二轮）：
- 心声**不是决策**，是一个「按钮」：桌宠可以按，也可以不按。
- 整合层只负责把多个直觉的苗头收成**一条可选念头**，不替 LLM 选动作、
  不替它组织语言、也不决定要不要让用户看见。
- 每个直觉按 `p_emit + boost × focus权重` 决定是否浮现（默认概率很低）。
- 只有**足够强**（score >= STRONG）的苗头才值得成为按钮；弱信号直接丢，
  避免每 4 秒刷一条「想安静待着」。
- 一致 → 一条念头；矛盾（两个强信号方向差 >= CONFLICT_SPREAD）→ 犹豫。
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional

from .registry import IntuitionRegistry

# 每种直觉的「苗头」措辞。只描述倾向，不描述动作——动作由 LLM 决定。
VOICE_BY_KIND: Dict[str, str] = {
    "emotion": "有点在意用户现在的状态",
    "space": "注意到旁边有个可以碰的东西",
    "habit": "想动一下",
    "custom": "有个说不清的念头",
}

STRONG = 0.35          # 低于此分数的直觉不构成"按钮"
CONFLICT_SPREAD = 0.5  # 两个强苗头差这么多 → 犹豫


def _voice_for(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    strong = [p for p in parts if float(p.get("score") or 0) >= STRONG]
    if not strong:
        # 只有弱信号：不产生念头（安静不是失败，是默认）
        return {"status": "quiet", "voice": "", "kind": "", "confidence": 0.0, "avg": 0.0, "parts": []}

    scores = [float(p["score"]) for p in strong]
    spread = max(scores) - min(scores) if len(strong) >= 2 else 0.0
    total_w = sum(float(p["weight"]) for p in strong) or 1.0
    avg = sum(float(p["score"]) * float(p["weight"]) for p in strong) / total_w

    if len(strong) >= 2 and spread >= CONFLICT_SPREAD:
        voice = "心里有点犹豫，两个方向在拉扯"
        kind = "conflict"
    else:
        dominant = max(strong, key=lambda p: float(p["score"]) * float(p["weight"]))
        kind = str(dominant.get("kind") or "custom")
        voice = VOICE_BY_KIND.get(kind, VOICE_BY_KIND["custom"])

    # 置信 = 一致度 × 倾向强度；犹豫时自然更低
    confidence = max(0.0, min(1.0, (1.0 - spread) * (0.5 + abs(avg - 0.5))))
    return {
        "status": "ok",
        "voice": voice,
        "kind": kind,
        "confidence": round(confidence, 2),
        "avg": round(avg, 2),
        "parts": strong,
    }


def integrate(
    registry: IntuitionRegistry,
    state: Dict[str, Any],
    needs: Dict[str, Any],
    rng: Optional[random.Random] = None,
) -> Dict[str, Any]:
    """返回 {status, voice, kind, confidence, avg, parts}。

    `voice` 只是一条可选念头，调用方（瞬忆/系统提示）必须明确告诉模型：
    可以采纳、可以忽略、也可以自己另想。
    """
    rng = rng or random
    parts: List[Dict[str, Any]] = []
    focus = needs.get("focus") or []
    boost = float(needs.get("boost") or 0.0)

    for name, it in registry.items().items():
        focus_boost = 1.2 if it.kind in focus else 0.25
        p = min(0.95, float(it.p_emit) + boost * focus_boost)
        if rng.random() > p:
            continue
        try:
            score = float(it.scorer(state))
        except Exception:
            continue
        score = max(0.0, min(1.0, score))
        parts.append({
            "name": name,
            "kind": it.kind,
            "score": round(score, 2),
            "weight": float(it.weight),
        })

    return _voice_for(parts)
