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

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
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
    # 只有看得见的刚体才让空间直觉兴奋（视锥外的东西不算"旁边有东西"）
    near = [
        b for b in bodies
        if b.get("in_fov", True) and float(b.get("dist", 9999)) < 400
    ]
    return min(1.0, 0.3 + 0.2 * len(near))


def _habit_intuition(state: Dict[str, Any]) -> float:
    recent = float(state.get("recent_actions") or 0)
    idle = float(state.get("idle_sec") or 0)
    return min(1.0, 0.15 + 0.08 * recent + min(0.3, idle / 3600))


_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff-]{1,40}$")

# 自创直觉是模型写的纯函数，只能拿到这些内置；没有 import / open / eval。
_SAFE_BUILTINS = {
    "len": len, "str": str, "float": float, "int": int, "min": min, "max": max,
    "sum": sum, "any": any, "all": all, "abs": abs, "round": round, "bool": bool,
    "list": list, "dict": dict, "tuple": tuple, "set": set, "enumerate": enumerate,
    "zip": zip, "range": range, "sorted": sorted, "isinstance": isinstance,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "True": True, "False": False, "None": None,
}


def compile_scorer(code: str, name: str) -> Callable[[Dict[str, Any]], float]:
    """Compile the model-authored `def intuition(state): ...` safely.

    Reuses the plugin AST policy (imports/calls/dunder restrictions) and
    runs with a tiny builtins table. This is a policy gate, not a process
    sandbox — same honest boundary as plugin_forge.
    """
    from ..plugin_policy import check_source

    code = (code or "").strip()
    if not code:
        raise ValueError("code is empty")
    violations = check_source(code)
    if violations:
        raise ValueError("; ".join(violations[:3]))
    ns: Dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}
    exec(compile(code, f"<intuition:{name}>", "exec"), ns, ns)
    fn = ns.get("intuition") or ns.get("score")
    if not callable(fn):
        raise ValueError("code must define `def intuition(state): ...`")
    return fn


@dataclass
class Intuition:
    name: str
    kind: str
    description: str
    scorer: Callable[[Dict[str, Any]], float]
    p_emit: float = 0.1
    weight: float = 1.0
    builtin: bool = False
    code: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


class IntuitionRegistry:
    MAX_CUSTOM = 16

    def __init__(self) -> None:
        self._items: Dict[str, Intuition] = {}
        self._custom_path: Optional[Path] = None
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
        self.save_custom()
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
                "has_code": bool(it.code),
            }
            for it in self._items.values()
        ]

    # ── 自创直觉：持久化 + 安全编译 ─────────────────────────────────
    def add_custom(
        self,
        name: str,
        description: str,
        code: str,
        kind: str = "custom",
        p_emit: float = 0.1,
        weight: float = 1.0,
    ) -> Dict[str, Any]:
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            return {"status": "error", "message": "name must be 1-40 chars (letters/digits/_/-/CJK)"}
        if name in self._items:
            return {"status": "error", "message": f"'{name}' already exists"}
        try:
            scorer = compile_scorer(code, name)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
        result = self.register(Intuition(
            name=name,
            kind=(kind or "custom").strip() or "custom",
            description=str(description or "")[:200],
            scorer=scorer,
            p_emit=max(0.0, min(0.9, float(p_emit))),
            weight=max(0.1, min(3.0, float(weight))),
            builtin=False,
            code=code,
        ))
        if result.get("status") == "ok":
            self.save_custom()
        return result

    def clear_custom(self) -> None:
        for name in [n for n, it in self._items.items() if not it.builtin]:
            self._items.pop(name, None)

    def export_custom(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": it.name,
                "kind": it.kind,
                "description": it.description,
                "code": it.code,
                "p_emit": it.p_emit,
                "weight": it.weight,
            }
            for it in self._items.values()
            if not it.builtin and it.code
        ]

    def save_custom(self, path: Optional[Path] = None) -> bool:
        target = Path(path) if path else self._custom_path
        if target is None:
            return False
        self._custom_path = target
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "intuitions": self.export_custom()}
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception:
            return False

    def load_custom(self, path: Optional[Path]) -> int:
        if path is None:
            return 0
        self._custom_path = Path(path)
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return 0
        loaded = 0
        for item in data.get("intuitions") or []:
            try:
                name = str(item.get("name") or "").strip()
                if not name or name in self._items or not _NAME_RE.match(name):
                    continue
                scorer = compile_scorer(item.get("code") or "", name)
                self.register(Intuition(
                    name=name,
                    kind=str(item.get("kind") or "custom"),
                    description=str(item.get("description") or "")[:200],
                    scorer=scorer,
                    p_emit=max(0.0, min(0.9, float(item.get("p_emit") or 0.1))),
                    weight=max(0.1, min(3.0, float(item.get("weight") or 1.0))),
                    builtin=False,
                    code=str(item.get("code") or ""),
                ))
                loaded += 1
            except Exception:
                continue
        return loaded
