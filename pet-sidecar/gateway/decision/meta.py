"""meta.py - 元直觉：判断"要不要提高决策概率、提高哪类"

设计（2026-10-19，用户定稿）：
- 元直觉**不判断选项类决策**（不决定走几步）——那由直觉自己做
- 元直觉只输出两件事：
    boost: 0..1   —— 要不要提高默认决策概率
    focus: [kind] —— 哪类直觉该活跃（action/emotion/space/habit）
- 原语匹配：boost 是 Score，focus 是 Choice（离散标签），不做跨类型判断
"""

from __future__ import annotations

from typing import Any, Dict, List

from .registry import NEGATIVE_HINTS


def needs_decision(state: Dict[str, Any]) -> Dict[str, Any]:
    """返回 {boost: float, focus: List[str], signals: List[str]}"""
    msg = str(state.get("last_user_msg") or "")
    idle = float(state.get("idle_sec") or 0)
    bodies = state.get("bodies") or []
    recent = int(state.get("recent_actions") or 0)

    signals: List[str] = []
    focus: List[str] = []
    boost = 0.1  # 平时安静

    neg = sum(1 for w in NEGATIVE_HINTS if w in msg)
    if neg:
        signals.append("negative_mood")
        boost = max(boost, 0.9)
        focus.append("emotion")
    if msg and len(msg) <= 4:
        signals.append("short_reply")
        boost = max(boost, 0.7)
        focus.append("emotion")
    if idle > 600:
        signals.append("long_idle")
        boost = max(boost, 0.6)
        focus.append("habit")
    if bodies:
        signals.append("bodies_present")
        boost = max(boost, 0.5)
        focus.append("space")
    if recent >= 3:
        signals.append("active_body")
        boost = max(boost, 0.5)
        focus.append("space")

    # 去重保序
    seen = set()
    focus_unique = []
    for k in focus:
        if k not in seen:
            seen.add(k)
            focus_unique.append(k)

    return {
        "boost": round(min(1.0, boost), 2),
        "focus": focus_unique or ["emotion"],
        "signals": signals,
    }
