"""registry.py - 直觉注册表（内置 3 个 + AI 自创）

设计（2026-10-19）：
- 每个直觉 = 纯函数 (state) -> 0..1，无副作用
- 内置三个（不可删）：情绪直觉 / 空间直觉 / 习惯直觉
- AI 可通过 pet_add_intuition 自创（来源 custom），可调 p_emit / weight
- 所有直觉**默认一直运行**，只是`p_emit`（浮现概率）默认很低

state 约定（由决策循环传入的"状态卡"）：
  last_user_msg, mood_index, idle_sec, bodies, recent_actions, time_of_day
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

NEGATIVE_HINTS = ("累", "烦", "难过", "生气", "压力", "加班", "困", "焦虑", "不想", "难受")


def _emotion_intuition(state: Dict[str, Any]) -> float:
    msg = str(state.get("last_user_msg") or "")
    if not msg:
        return 0.0
    neg = sum(1 for w in NEGATIVE_HINTS if w in msg)
    if neg:
        return min(1.0, 0.5 + 0.15 * neg)
    if len(msg) <= 4:
        return 0.2
    return 0.5


def _space_intuition(state: Dict[str, Any]) -> float:
    bodies = state.get("bodies") or []
    if not bodies:
        return 0.0
    near = [b for b in bodies if float(b.get("dist", 9999)) < 400]
    return min(1.0, 0.3 + 0.2 * len(near))


def _habit_intuition(state: Dict[str, Any]) -> float:
    recent = float(state.get("recent_actions") or 0)
    idle = float(state.get("idle_sec") or 0)
    return min(1.0, 0.15 + 0.08 * recent + min(0.3, idle / 3600))


@dataclass
class Intuition:
    name: str
    kind: str
    description: str
    scorer: Callable[[Dict[str, Any]], float]
    p_emit: float = 0.1
    weight: float = 1.0
    builtin: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)


class IntuitionRegistry:
    MAX_CUSTOM = 16

    def __init__(self) -> None:
        self._items: Dict[str, Intuition] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        self.register(Intuition(
            name="emotion",
            kind="emotion",
            description="用户语气里的疲惫/负面信号，影响要不要安静关心",
            scorer=_emotion_intuition,
            p_emit=0.08,
            weight=1.2,
            builtin=True,
        ))
        self.register(Intuition(
            name="space",
            kind="space",
            description="视野附近有多少可玩的刚体/对象",
            scorer=_space_intuition,
            p_emit=0.06,
            weight=1.0,
            builtin=True,
        ))
        self.register(Intuition(
            name="habit",
            kind="habit",
            description="最近动作与闲置时长的习惯倾向",
            scorer=_habit_intuition,
            p_emit=0.10,
            weight=0.9,
            builtin=True,
        ))

    def register(self, intuition: Intuition) -> Dict[str, Any]:
        name = (intuition.name or "").strip()
        if not name:
            return {"status": "error", "message": "name required"}
        if intuition.builtin and name in self._items:
            return {"status": "error", "message": "builtin already registered"}
        if not intuition.builtin:
            custom = [i for i in self._items.values() if not i.builtin]
            if len(custom) >= self.MAX_CUSTOM and name not in self._items:
                return {"status": "error", "message": f"custom intent limit ({self.MAX_CUSTOM}) reached"}
            intuition.kind = intuition.kind or "custom"
        self._items[name] = intuition
        return {"status": "ok", "name": name}

    def remove(self, name: str) -> Dict[str, Any]:
        it = self._items.get(name)
        if not it:
            return {"status": "error", "message": "not found"}
        if it.builtin:
            return {"status": "error", "message": "builtin intuitions cannot be removed"}
        del self._items[name]
        return {"status": "ok"}

    def get(self, name: str) -> Optional[Intuition]:
        return self._items.get(name)

    def items(self) -> Dict[str, Intuition]:
        return dict(self._items)

    def describe(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": it.name,
                "kind": it.kind,
                "description": it.description,
                "p_emit": it.p_emit,
                "weight": it.weight,
                "builtin": it.builtin,
            }
            for it in self._items.values()
        ]
