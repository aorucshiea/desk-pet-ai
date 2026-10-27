"""loop.py - 决策循环：直觉一直在跑，低概率浮现

设计（2026-10-19）：
- 每 ~4s tick：元直觉 → 直觉浮现 → 整合层 → 一份心声写入瞬时记忆
- 默认低概率：平时 boost=0.1，几乎不打扰
- 只写瞬时记忆（脑内），**不执行动作**——动作是否发生由 LLM 决定（不梦游）
- 依赖注入：collect_state / ephemeral / registry，避免和 server 强耦合
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from typing import Any, Callable, Dict, Optional

from .. import telemetry
from .integrate import integrate
from .meta import needs_decision
from .registry import IntuitionRegistry

# How fast a fresh inner voice loses its power to pull the next proactive
# chat closer. Ten minutes to half: pressure is a nudge, not a queue.
PRESSURE_HALF_LIFE_SECONDS = 600.0
# Only a reasonably strong voice may move the schedule. Weak/conflicted
# intuitions stay in ephemeral memory as buttons, but never shorten gaps.
PRESSURE_MIN_CONFIDENCE = 0.5


class DecisionLoop:
    def __init__(
        self,
        registry: IntuitionRegistry,
        ephemeral,
        collect_state: Callable[[], Dict[str, Any]],
        interval_sec: float = 4.0,
        now_fn: Optional[Callable[[], float]] = None,
        last_user_ts_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self.registry = registry
        self.ephemeral = ephemeral
        self.collect_state = collect_state
        self.interval_sec = interval_sec
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self._ticks = 0
        self.last: Dict[str, Any] = {}
        self.last_error = ""
        # Pressure state: the newest voice, its confidence and when it was
        # heard. Kept out of `last` so the existing snapshot stays stable.
        self._now = now_fn or time.time
        self._last_user_ts_fn = last_user_ts_fn
        self._state_user_ts = 0.0
        self._voice_at: Optional[float] = None
        self._voice_confidence = 0.0

    async def _run(self) -> None:
        while not self._stop:
            try:
                await asyncio.sleep(self.interval_sec)
                if self._stop:
                    break
                self._ticks += 1
                telemetry.inc("decision.ticks")
                # 1-hour clear is enforced by the ticker (and again by
                # format_for_prompt as a safety net).
                self.ephemeral.maybe_reset()
                if self._ticks % 15 == 0:
                    self.ephemeral.refresh_dir_snapshot()
                state = self.collect_state() or {}
                if inspect.isawaitable(state):
                    state = await state
                self._note_user_ts(state)
                needs = needs_decision(state)
                result = integrate(self.registry, state, needs)
                self.last = {"needs": needs, "result": result}
                voice = result.get("voice") if result.get("status") == "ok" else ""
                if voice:
                    telemetry.inc("decision.voices")
                    # 一条心声 = 一个可按可不按的按钮。只写瞬忆，不执行动作。
                    self.ephemeral.add_thought(
                        voice,
                        source=result.get("kind") or "integration",
                        confidence=result.get("confidence", 0),
                    )
                    self._record_voice(result.get("confidence", 0))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.last_error = str(exc)
                continue

    # ── 直觉压力（潜意识只挪动时机，不决定动作/措辞）────────────────
    def _note_user_ts(self, state: Any) -> None:
        """Pick up a user-message timestamp from the collected state as a
        fallback source; a live last_user_ts_fn still takes precedence."""
        if not isinstance(state, dict):
            return
        try:
            ts = float(state.get("last_user_ts") or 0.0)
        except (TypeError, ValueError):
            return
        if math.isfinite(ts) and ts > self._state_user_ts:
            self._state_user_ts = ts

    def _record_voice(self, confidence: Any) -> None:
        try:
            value = float(confidence or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if not math.isfinite(value):
            value = 0.0
        self._voice_at = self._now()
        self._voice_confidence = max(0.0, min(1.0, value))

    def _latest_user_ts(self) -> float:
        if self._last_user_ts_fn is not None:
            try:
                value = float(self._last_user_ts_fn() or 0.0)
                if math.isfinite(value):
                    return value
            except Exception:
                pass
        return self._state_user_ts

    def pressure(self, now: Optional[float] = None) -> float:
        """How strongly the newest voice wants the next proactive opening
        moved earlier, in [0, 1].

        Zero when there is no voice, the voice was too weak, or the user
        has spoken since it appeared. Otherwise it decays with a 10-minute
        half-life — a fresh hunch nudges, an old one fades.
        """
        if self._voice_at is None or self._voice_confidence < PRESSURE_MIN_CONFIDENCE:
            return 0.0
        try:
            current = float(self._now() if now is None else now)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(current):
            return 0.0
        if self._latest_user_ts() > self._voice_at:
            return 0.0
        age = max(0.0, current - self._voice_at)
        decay = 0.5 ** (age / PRESSURE_HALF_LIFE_SECONDS)
        return max(0.0, min(1.0, self._voice_confidence * decay))

    def consume_pressure(self, now: Optional[float] = None) -> float:
        """Read and clear the pressure. The server calls this once when it
        turns pressure into a shorter gap, so one hunch cannot keep
        shortening every subsequent gap forever."""
        value = self.pressure(now)
        self._voice_at = None
        self._voice_confidence = 0.0
        return value

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
