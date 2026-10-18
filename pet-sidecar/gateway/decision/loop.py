"""loop.py - 决策循环：直觉一直在跑，低概率浮现

设计（2026-10-19）：
- 每 ~4s tick：元直觉 → 直觉浮现 → 整合层 → 一份心声写入瞬时记忆
- 默认低概率：平时 boost=0.1，几乎不打扰
- 只写瞬时记忆（脑内），**不执行动作**——动作是否发生由 LLM 决定（不梦游）
- 依赖注入：collect_state / ephemeral / registry，避免和 server 强耦合
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional

from .integrate import integrate
from .meta import needs_decision
from .registry import IntuitionRegistry


class DecisionLoop:
    def __init__(
        self,
        registry: IntuitionRegistry,
        ephemeral,
        collect_state: Callable[[], Dict[str, Any]],
        interval_sec: float = 4.0,
    ) -> None:
        self.registry = registry
        self.ephemeral = ephemeral
        self.collect_state = collect_state
        self.interval_sec = interval_sec
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self.last: Dict[str, Any] = {}

    async def _run(self) -> None:
        while not self._stop:
            try:
                await asyncio.sleep(self.interval_sec)
                if self._stop:
                    break
                state = self.collect_state() or {}
                needs = needs_decision(state)
                result = integrate(self.registry, state, needs)
                self.last = {"needs": needs, "result": result}
                if result.get("status") == "ok":
                    for t in result.get("thoughts") or []:
                        self.ephemeral.add_thought(t, source="integration", confidence=result.get("confidence", 0))
            except asyncio.CancelledError:
                break
            except Exception:
                continue

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = False
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None
