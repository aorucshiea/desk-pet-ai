"""decision - 决策层（System 1）

模块：
- dice.py      骰子（决定式/选择式/权重式）
- registry.py  直觉注册表（内置情绪/空间/习惯 + AI 自创）
- meta.py      元直觉（只判断"要不要提高概率/提高哪类"）
- integrate.py 整合层（丘脑：多直觉 → 一份心声）
- loop.py      决策循环（一直运行、低概率浮现）
- body_log.py  身体日志（动作的自我叙事）
"""

from .dice import PRESETS, roll, steps_from_label
from .registry import Intuition, IntuitionRegistry
from .meta import needs_decision
from .integrate import integrate
from .body_log import BodyLog
from .loop import DecisionLoop

__all__ = [
    "PRESETS",
    "roll",
    "steps_from_label",
    "Intuition",
    "IntuitionRegistry",
    "needs_decision",
    "integrate",
    "BodyLog",
    "DecisionLoop",
]
