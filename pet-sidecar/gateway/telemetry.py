"""telemetry.py - 潜意识行为遥测（B-10）。

闪现率、回忆成功率、共鸣命中、目录长度、首 token 延迟……这些常数以前
全靠手感。这里提供一个**零依赖、线程安全、可持久化**的计数器/仪表/
计时集合，各器官只负责上报，`GET /api/memory/stats` 负责让人看见。

约束：
- 上报绝不能弄坏主链路：所有调用点都应包 try/except，或调用本模块的
  安全包装（内部已不抛异常）。
- 默认内存态；``attach(memory_dir)`` 后每 60s 落一次 ``telemetry.json``，
  重启后计数接着走。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_FLUSH_SECONDS = 60.0
_RECENT_KEEP = 50


class Telemetry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: Dict[str, float] = {}
        self.gauges: Dict[str, float] = {}
        self._recent: Dict[str, List[float]] = {}
        self.started_at = time.time()
        self._dir: Optional[Path] = None
        self._last_flush = 0.0
        self._dirty = False

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def attach(self, memory_dir) -> None:
        with self._lock:
            self._dir = Path(memory_dir)
            try:
                data = json.loads(
                    (self._dir / "telemetry.json").read_text(encoding="utf-8")
                )
                for key, value in (data.get("counters") or {}).items():
                    self.counters[key] = self.counters.get(key, 0.0) + float(value)
                for key, value in (data.get("gauges") or {}).items():
                    self.gauges[key] = float(value)
            except Exception:
                pass

    def flush(self) -> None:
        with self._lock:
            if self._dir is None:
                return
            payload = {
                "counters": dict(self.counters),
                "gauges": dict(self.gauges),
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            path = self._dir / "telemetry.json"
            self._dirty = False
            self._last_flush = time.time()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

    def _maybe_flush(self) -> None:
        if self._dirty and (time.time() - self._last_flush) >= _FLUSH_SECONDS:
            self.flush()

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------
    def inc(self, name: str, n: float = 1.0) -> None:
        try:
            with self._lock:
                self.counters[name] = self.counters.get(name, 0.0) + n
                self._dirty = True
            self._maybe_flush()
        except Exception:
            pass

    def gauge(self, name: str, value: float) -> None:
        try:
            with self._lock:
                self.gauges[name] = float(value)
                self._dirty = True
            self._maybe_flush()
        except Exception:
            pass

    def observe(self, name: str, seconds: float) -> None:
        """Record one duration; keeps count/total plus a small recent window."""
        try:
            value = max(0.0, float(seconds))
            with self._lock:
                self.counters[name + ".count"] = self.counters.get(name + ".count", 0.0) + 1
                self.counters[name + ".total_seconds"] = (
                    self.counters.get(name + ".total_seconds", 0.0) + value
                )
                recent = self._recent.setdefault(name, [])
                recent.append(value)
                if len(recent) > _RECENT_KEEP:
                    del recent[:-_RECENT_KEEP]
                self.gauges[name + ".last_seconds"] = value
                self._dirty = True
            self._maybe_flush()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            counters = dict(self.counters)
            gauges = dict(self.gauges)
            recent = {k: list(v) for k, v in self._recent.items()}
            started = self.started_at

        timings: Dict[str, Any] = {}
        for name, values in recent.items():
            count = counters.get(name + ".count", 0.0)
            total = counters.get(name + ".total_seconds", 0.0)
            ordered = sorted(values)
            p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] if ordered else 0.0
            timings[name] = {
                "count": int(count),
                "total_seconds": round(total, 3),
                "avg_seconds": round(total / count, 3) if count else 0.0,
                "last_seconds": round(values[-1], 3) if values else 0.0,
                "p95_recent_seconds": round(p95, 3),
            }

        def _rate(num: str, den: str) -> Optional[float]:
            d = counters.get(den, 0.0)
            if not d:
                return None
            return round(counters.get(num, 0.0) / d, 4)

        return {
            "ok": True,
            "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
            "uptime_seconds": int(time.time() - started),
            "counters": {k: (int(v) if float(v).is_integer() else round(v, 3)) for k, v in counters.items()},
            "gauges": {k: round(v, 4) for k, v in gauges.items()},
            "timings": timings,
            "rates": {
                "recall_success": _rate("recall.success", "recall.attempts"),
                "resonance_hit": _rate("resonance.hits", "resonance.calls"),
                "mem_block_present": (
                    round(1.0 - counters.get("memory.extraction.missing_block", 0.0) / counters["memory.extraction.turns"], 4)
                    if counters.get("memory.extraction.turns")
                    else None
                ),
                "decision_voice_per_tick": _rate("decision.voices", "decision.ticks"),
            },
        }


_instance = Telemetry()


def attach(memory_dir) -> None:
    _instance.attach(memory_dir)


def inc(name: str, n: float = 1.0) -> None:
    _instance.inc(name, n)


def gauge(name: str, value: float) -> None:
    _instance.gauge(name, value)


def observe(name: str, seconds: float) -> None:
    _instance.observe(name, seconds)


def snapshot() -> Dict[str, Any]:
    return _instance.snapshot()


def flush() -> None:
    _instance.flush()
