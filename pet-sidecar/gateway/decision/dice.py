"""dice.py - 骰子：AI 用决策模型掷骰子（三种形式）

设计（2026-10-19，用户定稿）：
- **决定式（binary）**：两个面（走/不走），带概率
- **选择式（choice）**：N 个等概率面（走1/2/3/4）
- **权重式（weighted）**：N 个面 + 各自权重（走100步 0.1）

输出是**确定结果**（"走3步"），不是概率包——AI 拿到就能执行。
骰子内部完成"打分 + 采样"，这就是"掷骰子"。
"""

from __future__ import annotations

import random
from typing import Any, Dict, Optional


PRESETS: Dict[str, Dict[str, Any]] = {
    "walk_short": {"type": "binary", "p": 0.6, "yes": "走两步", "no": "待着"},
    "walk_steps": {"type": "choice", "options": [1, 2, 3, 4]},
    "walk_long": {
        "type": "weighted",
        "options": {"走1步": 0.4, "走2步": 0.3, "走3步": 0.2, "走10步": 0.08, "走100步": 0.1},
    },
    "look_dirs": {"type": "choice", "options": ["看左", "看前", "看右"]},
}


def roll(spec: Optional[Dict[str, Any]] = None, rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """按骰子定义采样，返回确定结果。

    spec:
      {"type":"binary","p":0.6,"yes":"走","no":"不走"}
      {"type":"choice","options":[1,2,3,4]}
      {"type":"weighted","options":{"走1步":0.4,"走100步":0.1}}
      或 {"preset":"walk_steps"}
    """
    rng = rng or random
    if not spec:
        return {"status": "error", "message": "empty dice spec"}
    if isinstance(spec, str):
        spec = PRESETS.get(spec)
        if not spec:
            return {"status": "error", "message": f"unknown preset {spec}"}
    if "preset" in spec:
        base = PRESETS.get(str(spec["preset"]))
        if not base:
            return {"status": "error", "message": f"unknown preset {spec['preset']}"}
        spec = base

    kind = str(spec.get("type") or "binary")
    try:
        if kind == "binary":
            p = float(spec.get("p", 0.5))
            p = max(0.0, min(1.0, p))
            hit = rng.random() < p
            return {
                "status": "ok",
                "type": "binary",
                "result": hit,
                "label": spec.get("yes" if hit else "no") or ("是" if hit else "否"),
                "p": round(p, 3),
            }
        if kind == "choice":
            options = list(spec.get("options") or [])
            if not options:
                return {"status": "error", "message": "choice needs options"}
            rng.shuffle(options)
            return {"status": "ok", "type": "choice", "result": options[0], "label": str(options[0])}
        if kind == "weighted":
            options = spec.get("options") or {}
            if not isinstance(options, dict) or not options:
                return {"status": "error", "message": "weighted needs an options dict"}
            labels = list(options.keys())
            weights = [max(0.0, float(options[k] or 0)) for k in labels]
            if sum(weights) <= 0:
                return {"status": "error", "message": "weights must sum > 0"}
            picked = rng.choices(labels, weights=weights, k=1)[0]
            return {"status": "ok", "type": "weighted", "result": picked, "label": str(picked)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    return {"status": "error", "message": f"unknown dice type {kind}"}


def steps_from_label(label: str) -> int:
    """从"走N步"里解析步数（给执行层用）。"""
    import re
    if not label:
        return 0
    m = re.search(r"(\d+)", str(label))
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 0
    if "走" in str(label):
        return 2
    return 0
