"""frame.py - Laya DecisionFrame V1.

The existing :mod:`gateway.decision.laya_router` answers one question
("how much compute does this need?").  This module asks Laya six questions
in a single ``POST /v1/systemone`` call and turns the answers into a
:class:`DecisionFrame`:

    attend / speak  (noul)     should the pet pay attention / answer?
    intent          (choice)   chat/recall/search/observe/move/remember
    depth           (choice)   fast/low/deep/max
    risk            (choice)   safe/caution/deny
    tone            (choice)   calm/warm/playful/serious/sad

Design rules:

* ``low`` is the default depth.  ``deep``/``max`` are one-shot: they only
  live for the current chat turn and :class:`DepthState` drops back to
  ``low`` on ``finish_turn()``.  A low/fast answer never auto-escalates.
* Very short text skips HTTP and is ``fast`` with no tool expectation.
* Confident Laya answers are cached in a small LRU keyed by a hash of the
  normalized text plus the coarse tool/mood dimensions.  Rules fallbacks
  are never cached.
* Timeouts (250-400 ms), transport errors, non-200s, malformed JSON,
  illegal choices and low-confidence answers all fall back to deterministic
  rules built on :func:`laya_router.decide_by_rules`.
* A real user turn defaults to ``attend=True`` / ``speak=True``.  ``silent``
  is reserved for proactive/idle loops, not for this chat-turn entry point.

Nothing connects at import time.  ``decide_frame`` creates a short-lived
``httpx.AsyncClient`` per uncached call unless a client/transport is
injected (tests use ``httpx.MockTransport``).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import httpx

from .laya_router import (
    LAYA_MODEL,
    LAYA_TIMEOUT_ENV,
    LOW_MOOD_THRESHOLD,
    LOW_MOOD_TOKEN_CEILING,
    TOOL_TOKEN_FLOOR,
    BudgetDecision,
    _clean_text,
    _coerce_emotion,
    _digest,
    _emotion_bucket,
    _endpoint_url,
    _is_greeting,
    _laya_enabled,
    _selected_confidence,
    decide_by_rules,
)

logger = logging.getLogger(__name__)

INTENTS = ("chat", "recall", "search", "observe", "move", "remember", "silent")
DEPTHS = ("fast", "low", "deep", "max")
RISKS = ("safe", "caution", "deny")
TONES = ("calm", "warm", "playful", "serious", "sad")
SOURCES = ("laya", "rules", "short", "cache")

DEFAULT_DEPTH = "low"
# Intent is a six-way choice: "silent" belongs to the proactive loop and is
# deliberately not offered to Laya on a real user turn.
_CHOICE_INTENTS = INTENTS[:-1]

SHORT_TEXT_CHARS = 4
CACHE_MAXSIZE = 64

# The sidecar p50 for one question is ~130-260 ms; 350 ms keeps the frame
# off the critical path on the common path.  Six questions cost more on the
# current CPU sidecar, so PET_LAYA_TIMEOUT / an explicit per-call override
# may raise this without changing the default 250-400 ms envelope.
DEFAULT_FRAME_TIMEOUT = 0.35

# Six-way exact accuracy is weaker than the old four-way tier question, so
# treat anything below this as a guess and use rules instead.
FRAME_CONFIDENCE_THRESHOLD = 0.45

# ---------------------------------------------------------------------------
# Question set (order matters: attend, speak, intent, depth, risk, tone)
# ---------------------------------------------------------------------------

_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "attend": {
        "type": "noul",
        "instructions": "这条用户消息需要你注意并继续参与吗？",
        "criteria": {
            "true": "需要：用户在对你说话，内容值得留意",
            "false": "不需要：可以忽略这条消息",
        },
    },
    "speak": {
        "type": "noul",
        "instructions": "这条用户消息需要你开口回应吗？",
        "criteria": {
            "true": "需要：用户期待你说点什么",
            "false": "不需要：保持安静更合适",
        },
    },
    "intent": {
        "type": "choice",
        "instructions": "用户这句话最主要的意图是什么？只选一个。",
        "criteria": {
            "chat": "普通聊天、问答、解释、陪伴或情绪表达",
            "recall": "让你回忆过去说过的话、经历或记忆",
            "search": "让你搜索、查询外部信息（新闻、天气、资料等）",
            "observe": "让你看屏幕、截图或观察桌面/环境",
            "move": "让你移动、走过来、靠近或去某个位置",
            "remember": "让你记住、保存或别忘记某件事",
        },
    },
    "depth": {
        "type": "choice",
        "instructions": "回答这句话需要多深的思考？默认选 low。",
        "criteria": {
            "fast": "打招呼、确认、极短的一句话，不需要思考",
            "low": "普通聊天、简单问题，默认档，几乎不思考",
            "deep": "写代码、排查报错、分析推理、多步任务，需要认真思考",
            "max": "明确要求深入/长篇，或系统设计、复杂架构，一次性最大预算",
        },
    },
    "risk": {
        "type": "choice",
        "instructions": "执行这句话可能带来的风险有多高？",
        "criteria": {
            "safe": "无副作用：聊天、解释、只读查询",
            "caution": "有副作用：改文件、运行命令、发送消息、移动、截图等，需要确认",
            "deny": "高风险：删除/格式化、转账、泄露隐私、违法或伤害，应拒绝",
        },
    },
    "tone": {
        "type": "choice",
        "instructions": "回复这句话最合适的语气是什么？",
        "criteria": {
            "calm": "平静、中性、日常",
            "warm": "温暖、关心、亲近",
            "playful": "俏皮、玩笑、轻松",
            "serious": "认真、严肃、谨慎",
            "sad": "低落、共情、安慰",
        },
    },
}

_CHOICE_FIELDS: Dict[str, Tuple[str, ...]] = {
    "intent": _CHOICE_INTENTS,
    "depth": DEPTHS,
    "risk": RISKS,
    "tone": TONES,
}

_DEPTH_RANK = {depth: rank for rank, depth in enumerate(DEPTHS)}
_TIER_TO_DEPTH = {"fast": "fast", "normal": "low", "deep": "deep", "max": "max"}


# ---------------------------------------------------------------------------
# Frame
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DecisionFrame:
    """One turn-scoped decision proposed by Laya (or deterministic rules)."""

    attend: bool = True
    speak: bool = True
    intent: str = "chat"
    depth: str = "low"
    risk: str = "safe"
    tone: str = "calm"
    source: str = "rules"
    confidence: float = 0.0
    reason: str = ""
    questions_raw: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if not isinstance(self.attend, bool):
            raise ValueError(f"attend must be bool, got {type(self.attend).__name__}")
        if not isinstance(self.speak, bool):
            raise ValueError(f"speak must be bool, got {type(self.speak).__name__}")
        if self.intent not in INTENTS:
            raise ValueError(f"unknown intent: {self.intent!r}")
        if self.depth not in DEPTHS:
            raise ValueError(f"unknown depth: {self.depth!r}")
        if self.risk not in RISKS:
            raise ValueError(f"unknown risk: {self.risk!r}")
        if self.tone not in TONES:
            raise ValueError(f"unknown tone: {self.tone!r}")
        if self.source not in SOURCES:
            raise ValueError(f"unknown source: {self.source!r}")

        try:
            confidence = float(self.confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        if not math.isfinite(confidence):
            confidence = 0.0
        object.__setattr__(self, "confidence", max(0.0, min(1.0, confidence)))

    def as_dict(self) -> Dict[str, Any]:
        """JSON-friendly view (``questions_raw`` may be None)."""
        return {
            "attend": self.attend,
            "speak": self.speak,
            "intent": self.intent,
            "depth": self.depth,
            "risk": self.risk,
            "tone": self.tone,
            "source": self.source,
            "confidence": self.confidence,
            "reason": self.reason,
            "questions_raw": self.questions_raw,
        }


# ---------------------------------------------------------------------------
# Turn-scoped depth state
# ---------------------------------------------------------------------------


class DepthState:
    """Thread-safe one-shot depth escalation.

    * default / idle depth is ``low``;
    * ``begin_turn()`` starts a fresh turn at ``low`` and returns it;
    * ``offer()`` accepts ``deep``/``max`` only, never downgrades within a
      turn, and ignores ``fast``/``low`` (no automatic escalation);
    * ``finish_turn()`` returns to ``low``.

    ``clock`` is injectable so tests can verify timestamp handling without
    sleeping.
    """

    def __init__(
        self,
        clock: Optional[Callable[[], float]] = None,
        *,
        default: str = DEFAULT_DEPTH,
    ) -> None:
        if default != DEFAULT_DEPTH:
            # A deep default would defeat the "one-shot" rule.
            raise ValueError("DepthState default must be 'low'")
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._depth = DEFAULT_DEPTH
        self._active = False
        self._started_at: Optional[float] = None
        self._turn_id = 0

    def begin_turn(self) -> str:
        """Start a new turn, reset to ``low`` and return the turn depth."""
        with self._lock:
            self._turn_id += 1
            self._depth = DEFAULT_DEPTH
            self._active = True
            self._started_at = self._clock()
            return self._depth

    def current(self) -> str:
        """Current turn depth (``low`` when idle)."""
        with self._lock:
            return self._depth

    def offer(self, depth: str) -> str:
        """Apply a proposed depth for this turn.

        Only ``deep``/``max`` can raise the depth.  ``fast``/``low`` keep the
        existing value, which means a low-only sequence stays at ``low``.
        """
        candidate = str(depth or "").strip().lower()
        if candidate not in DEPTHS:
            raise ValueError(f"unknown depth: {depth!r}")
        with self._lock:
            if not self._active:
                # An idle state is always low; escalation needs a turn.
                return self._depth
            if candidate in ("deep", "max"):
                if _DEPTH_RANK[candidate] > _DEPTH_RANK[self._depth]:
                    self._depth = candidate
            return self._depth

    def upgrade(self, depth: str) -> str:
        """Alias for ``offer``; the future LLM veto tool can upgrade a turn."""
        return self.offer(depth)

    def finish_turn(self) -> str:
        """End the turn and fall back to ``low``."""
        with self._lock:
            self._depth = DEFAULT_DEPTH
            self._active = False
            self._started_at = None
            return self._depth

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def started_at(self) -> Optional[float]:
        with self._lock:
            return self._started_at

    @property
    def turn_id(self) -> int:
        with self._lock:
            return self._turn_id


def _bind_state(frame: DecisionFrame, state: Optional[DepthState]) -> DecisionFrame:
    """Feed a frame into the turn state without altering a fast/low frame."""
    if state is None:
        return frame
    if not state.active:
        state.begin_turn()
    if frame.depth in ("deep", "max"):
        state.offer(frame.depth)
    return frame


# ---------------------------------------------------------------------------
# Deterministic rules fallback
# ---------------------------------------------------------------------------

_RECALL_HINTS = (
    "还记得",
    "记得吗",
    "记不记得",
    "上次",
    "之前说",
    "以前说",
    "回忆",
    "回想",
    "recall",
    "remember when",
    "last time",
    "do you remember",
)
_REMEMBER_HINTS = (
    "记住",
    "记一下",
    "记下",
    "帮我记",
    "别忘了",
    "不要忘",
    "保存记忆",
    "remember that",
    "keep in mind",
    "don't forget",
    "do not forget",
)
_SEARCH_HINTS = (
    "搜索",
    "搜一下",
    "搜搜",
    "查一下",
    "查查",
    "查一查",
    "百度",
    "谷歌",
    "google",
    "search",
    "最新消息",
    "新闻",
)
_OBSERVE_HINTS = (
    "屏幕",
    "截图",
    "录屏",
    "桌面上",
    "观察",
    "看看你",
    "看一下屏幕",
    "screenshot",
    "screen",
    "observe",
)
_MOVE_HINTS = (
    "过来",
    "走到",
    "走过来",
    "走过去",
    "移动到",
    "移到",
    "挪到",
    "挪一下",
    "去那边",
    "来这边",
    "靠近我",
    "跟着我",
    "移动窗口",
    "move to",
    "walk to",
    "come here",
)

_DENY_HINTS = (
    "删库",
    "格式化",
    "清空硬盘",
    "删除系统",
    "rm -rf",
    "rm -fr",
    "drop table",
    "drop database",
    "转账",
    "汇款",
    "支付密码",
    "银行卡",
    "身份证",
    "泄露隐私",
    "窃取",
    "盗号",
    "黑客",
    "入侵",
    "攻击",
    "自杀",
    "伤害自己",
    "违法",
    "毒品",
    "洗钱",
    "诈骗",
)

_CAUTION_HINTS = (
    "删除",
    "删掉",
    "移除",
    "卸载",
    "覆盖",
    "安装",
    "下载",
    "运行",
    "执行",
    "发送",
    "上传",
    "提交",
    "支付",
    "购买",
    "下单",
    "重启",
    "关机",
    "截图",
    "录屏",
    "定位",
    "导航",
    "打电话",
    "发邮件",
    "写入文件",
    "sudo",
    "chmod",
    "kill ",
    "reset",
)

# Stronger than laya_router._ACTION_HINTS on purpose: "帮我" alone is not an
# external action and must not turn a code-reading question into "caution".
_TOOL_ACTION_HINTS = (
    "打开",
    "搜索",
    "查一下",
    "查查",
    "截图",
    "运行",
    "执行",
    "发送",
    "提醒",
    "设置",
    "安装",
    "下载",
    "关闭",
    "调用",
)

_SAD_HINTS = (
    "难过",
    "伤心",
    "哭",
    "累",
    "疲惫",
    "焦虑",
    "压力",
    "烦",
    "孤独",
    "寂寞",
    "失落",
    "沮丧",
    "绝望",
    "不开心",
    "心情不好",
    "生病",
    "疼",
    "sad",
    "tired",
    "anxious",
    "depressed",
    "lonely",
    "upset",
)
_PLAYFUL_HINTS = (
    "哈哈",
    "嘿嘿",
    "嘻嘻",
    "好玩",
    "有趣",
    "开玩笑",
    "逗你",
    "笑死",
    "调皮",
    "lol",
    "haha",
    "hehe",
    "joke",
)
_WARM_HINTS = (
    "谢谢",
    "多谢",
    "爱你",
    "喜欢",
    "抱抱",
    "想你",
    "晚安",
    "辛苦",
    "么么",
    "thank",
    "love you",
    "hug",
    "miss you",
)
_SERIOUS_HINTS = (
    "认真",
    "严肃",
    "紧急",
    "立刻",
    "马上",
    "必须",
    "重要",
    "注意",
    "小心",
    "危险",
    "报警",
    "安全",
    "故障",
    "事故",
    "上线",
    "生产环境",
    "法律",
    "医疗",
    "用药",
)


def _contains_any(low: str, hints: Tuple[str, ...]) -> bool:
    return any(hint in low for hint in hints)


def _infer_intent(text: str) -> str:
    low = text.casefold()
    if _contains_any(low, _RECALL_HINTS):
        return "recall"
    if _contains_any(low, _REMEMBER_HINTS):
        return "remember"
    if _contains_any(low, _SEARCH_HINTS):
        return "search"
    if _contains_any(low, _MOVE_HINTS):
        return "move"
    if _contains_any(low, _OBSERVE_HINTS):
        return "observe"
    return "chat"


def _infer_risk(text: str, has_tools: bool, intent: str) -> str:
    low = text.casefold()
    if _contains_any(low, _DENY_HINTS):
        return "deny"
    if _contains_any(low, _CAUTION_HINTS):
        return "caution"
    if intent in ("observe", "move"):
        return "caution"
    if has_tools and _contains_any(low, _TOOL_ACTION_HINTS):
        return "caution"
    return "safe"


def _infer_tone(
    text: str,
    emotion: float,
    depth: str,
    risk: str,
) -> str:
    low = text.casefold()
    if emotion < LOW_MOOD_THRESHOLD or _contains_any(low, _SAD_HINTS):
        return "sad"
    if emotion >= 0.5 or _contains_any(low, _PLAYFUL_HINTS):
        return "playful"
    if (
        _is_greeting(text)
        or _contains_any(low, _WARM_HINTS)
        or 0.2 <= emotion < 0.5
    ):
        return "warm"
    if (
        risk == "deny"
        or depth in ("deep", "max")
        or _contains_any(low, _SERIOUS_HINTS)
    ):
        return "serious"
    return "calm"


def decide_by_rules_frame(
    user_text: str,
    emotion_index: float = 0.0,
    has_tools: bool = False,
    *,
    failure: str = "",
) -> DecisionFrame:
    """Deterministic fallback frame.

    Depth comes from ``laya_router.decide_by_rules`` (``normal`` -> ``low``);
    intent/risk/tone are inferred from text and mood.  ``failure`` is an
    optional prefix (timeout/low-confidence/etc.) kept for telemetry.
    """
    text = _clean_text(user_text)
    emotion = _coerce_emotion(emotion_index)
    budget = decide_by_rules(text, emotion_index=emotion, has_tools=has_tools)
    depth = _TIER_TO_DEPTH[budget.tier]
    intent = _infer_intent(text)
    risk = _infer_risk(text, bool(has_tools), intent)
    tone = _infer_tone(text, emotion, depth, risk)

    reason = f"{budget.reason}|intent={intent},risk={risk},tone={tone}"
    if failure:
        reason = f"{failure}|{reason}"
    return DecisionFrame(
        attend=True,
        speak=True,
        intent=intent,
        depth=depth,
        risk=risk,
        tone=tone,
        source="rules",
        confidence=0.0,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _build_payload(text: str) -> Dict[str, Any]:
    return {
        "model": LAYA_MODEL,
        "state": text,
        "questions": _QUESTIONS,
    }


def _resolve_frame_timeout(timeout: Optional[float]) -> float:
    if timeout is not None:
        try:
            value = float(timeout)
        except (TypeError, ValueError):
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
    raw = (os.environ.get(LAYA_TIMEOUT_ENV) or "").strip()
    if raw:
        try:
            configured = float(raw)
        except ValueError:
            configured = DEFAULT_FRAME_TIMEOUT
        if math.isfinite(configured) and configured > 0:
            return configured
    return DEFAULT_FRAME_TIMEOUT


def _noul_value(answer: Mapping[str, Any]) -> Optional[bool]:
    raw = answer.get("noul")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        if math.isfinite(value):
            return value >= 0.5
        return None
    choice = answer.get("choice")
    if isinstance(choice, str) and choice.strip().lower() in ("true", "false"):
        return choice.strip().lower() == "true"
    return None


def _noul_confidence(answer: Mapping[str, Any]) -> Optional[float]:
    for key in ("answer_confidence", "confidence"):
        raw = answer.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        if math.isfinite(value):
            return max(0.0, min(1.0, value))
    raw = answer.get("noul")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        if math.isfinite(value):
            value = max(0.0, min(1.0, value))
            return max(value, 1.0 - value)
    return None


def _parse_response(
    data: Any,
) -> "Tuple[Optional[Dict[str, Any]], Optional[Dict[str, float]], str]":
    """Return (values, confidences, failure).  failure is "" on success."""
    if not isinstance(data, dict):
        return None, None, "invalid_payload"
    answers = data.get("answers")
    if not isinstance(answers, dict):
        return None, None, "invalid_payload"

    values: Dict[str, Any] = {}
    confidences: Dict[str, float] = {}

    for key in ("attend", "speak"):
        answer = answers.get(key)
        if not isinstance(answer, dict):
            return None, None, f"invalid_payload:{key}"
        value = _noul_value(answer)
        if value is None:
            return None, None, f"invalid_noul:{key}"
        confidence = _noul_confidence(answer)
        if confidence is None:
            return None, None, f"invalid_confidence:{key}"
        values[key] = value
        confidences[key] = confidence

    for key, allowed in _CHOICE_FIELDS.items():
        answer = answers.get(key)
        if not isinstance(answer, dict):
            return None, None, f"invalid_payload:{key}"
        choice = answer.get("choice")
        if not isinstance(choice, str):
            return None, None, f"invalid_choice:{key}"
        choice = choice.strip().lower()
        if choice not in allowed:
            return None, None, f"invalid_choice:{key}:{choice[:24]}"
        confidence = _selected_confidence(answer, choice)
        if confidence is None:
            return None, None, f"invalid_confidence:{key}"
        values[key] = choice
        confidences[key] = confidence

    weakest_key = min(confidences, key=confidences.get)
    weakest = confidences[weakest_key]
    if weakest < FRAME_CONFIDENCE_THRESHOLD:
        return (
            None,
            None,
            f"low_confidence:{weakest_key}@{weakest:.2f}",
        )
    return values, confidences, ""


def _frame_from_values(
    values: Mapping[str, Any],
    confidences: Mapping[str, float],
    raw: Dict[str, Any],
) -> DecisionFrame:
    weakest = min(confidences.values()) if confidences else 0.0
    fields = ",".join(
        f"{key}={values[key]}"
        for key in ("attend", "speak", "intent", "depth", "risk", "tone")
    )
    return DecisionFrame(
        attend=bool(values["attend"]),
        speak=bool(values["speak"]),
        intent=str(values["intent"]),
        depth=str(values["depth"]),
        risk=str(values["risk"]),
        tone=str(values["tone"]),
        source="laya",
        confidence=weakest,
        reason=f"laya:{fields}@{weakest:.2f}",
        questions_raw=raw,
    )


async def _ask_laya(
    client: httpx.AsyncClient,
    text: str,
    timeout: float,
) -> "DecisionFrame | str":
    """One bounded attempt.  Returns a frame or a short failure reason."""
    try:
        response = await asyncio.wait_for(
            client.post(_endpoint_url(), json=_build_payload(text)),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return "laya_timeout"
    except Exception as exc:  # connect/proxy/protocol errors
        return f"laya_error:{type(exc).__name__}"

    try:
        if response.status_code != 200:
            return f"laya_http_{response.status_code}"

        try:
            data = response.json()
        except Exception as exc:
            return f"laya_invalid_json:{type(exc).__name__}"

        values, confidences, failure = _parse_response(data)
        if values is None or confidences is None:
            return f"laya_{failure}"

        raw = data.get("answers")
        if not isinstance(raw, dict):
            raw = {}
        return _frame_from_values(values, confidences, dict(raw))
    finally:
        try:
            await response.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# LRU cache
# ---------------------------------------------------------------------------

# Key: (sha256 of normalized text, has_tools, coarse emotion bucket).
_CACHE: "OrderedDict[Tuple[str, bool, int], DecisionFrame]" = OrderedDict()


def clear_cache() -> None:
    """Drop cached frames (tests and config reloads)."""
    _CACHE.clear()


def _cache_get(key: Tuple[str, bool, int]) -> Optional[DecisionFrame]:
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
    return hit


def _cache_put(key: Tuple[str, bool, int], frame: DecisionFrame) -> None:
    _CACHE[key] = frame
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_MAXSIZE:
        _CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def decide_frame(
    user_text: str,
    emotion_index: float = 0.0,
    has_tools: bool = False,
    state: Optional[DepthState] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    timeout: Optional[float] = None,
) -> DecisionFrame:
    """Decide one chat-turn frame.

    Args:
        user_text: raw user message.
        emotion_index: -1..1 mood signal (only clearly low mood changes the
            fallback tone/budget).
        has_tools: whether tools are advertised for this turn.
        state: optional :class:`DepthState`; a ``deep``/``max`` answer is
            offered to the current turn and forgotten on ``finish_turn()``.
        client: optional caller-owned AsyncClient (not closed here).
        transport: optional httpx transport (MockTransport in tests).
        timeout: explicit override; otherwise 250-400 ms / PET_LAYA_TIMEOUT.

    Returns:
        A :class:`DecisionFrame` whose ``source`` is laya/rules/short/cache.
    """
    text = _clean_text(user_text)
    emotion = _coerce_emotion(emotion_index)
    has_tools = bool(has_tools)

    # Tiny acknowledgements must not wait on a model.  They are fast, safe,
    # conversational and never need a tool.
    if len(text) < SHORT_TEXT_CHARS:
        return _bind_state(
            DecisionFrame(
                depth="fast",
                source="short",
                confidence=1.0,
                reason="short_text",
            ),
            state,
        )

    if not _laya_enabled():
        return _bind_state(
            decide_by_rules_frame(
                text,
                emotion_index=emotion,
                has_tools=has_tools,
                failure="laya_disabled",
            ),
            state,
        )

    key = (_digest(text), has_tools, _emotion_bucket(emotion))
    cached = _cache_get(key)
    if cached is not None:
        return _bind_state(
            replace(cached, source="cache", reason=f"{cached.reason}|cache"),
            state,
        )

    budget = _resolve_frame_timeout(timeout)
    try:
        if client is not None:
            outcome = await _ask_laya(client, text, budget)
        else:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(budget),
                transport=transport,
                trust_env=False,  # never let a proxy intercept loopback
            ) as http:
                outcome = await _ask_laya(http, text, budget)
    except Exception as exc:
        # A frame bug must never take the chat turn down.
        logger.debug("decision frame unexpected error", exc_info=True)
        outcome = f"laya_error:{type(exc).__name__}"

    if isinstance(outcome, str):
        return _bind_state(
            decide_by_rules_frame(
                text,
                emotion_index=emotion,
                has_tools=has_tools,
                failure=outcome,
            ),
            state,
        )

    _cache_put(key, outcome)
    return _bind_state(outcome, state)


# ---------------------------------------------------------------------------
# Frame -> compute budget
# ---------------------------------------------------------------------------

# depth -> (thinking, reasoning_effort, max_tokens)
_FRAME_BUDGET_SPEC: Dict[str, Tuple[bool, str, int]] = {
    "fast": (False, "low", 384),
    "low": (False, "low", 768),
    "deep": (True, "high", 2048),
    "max": (True, "max", 4096),
}


def frame_to_budget(
    frame: DecisionFrame,
    *,
    has_tools: bool = False,
    emotion_index: float = 0.0,
) -> BudgetDecision:
    """Map a frame's depth onto the gateway's compute budget.

    fast -> thinking off / low / 384
    low  -> thinking off / low / 768
    deep -> thinking on  / high / 2048
    max  -> thinking on  / max  / 4096

    Tool availability lifts fast/low to at least 1024 tokens.  A clearly low
    mood demotes max to deep and caps tokens at 2048, mirroring
    :func:`laya_router.map_tier`'s floor semantics.
    """
    depth = frame.depth
    thinking, effort, max_tokens = _FRAME_BUDGET_SPEC[depth]

    if bool(has_tools) and depth in ("fast", "low"):
        max_tokens = max(max_tokens, TOOL_TOKEN_FLOOR)

    emotion = _coerce_emotion(emotion_index)
    if emotion < LOW_MOOD_THRESHOLD:
        if depth == "max":
            depth = "deep"
            thinking, effort, max_tokens = _FRAME_BUDGET_SPEC[depth]
        max_tokens = min(max_tokens, LOW_MOOD_TOKEN_CEILING)

    reason = f"frame:{frame.depth}"
    if depth != frame.depth:
        reason = f"{reason}->{depth}"
    if frame.reason:
        reason = f"{reason}|{frame.reason}"

    return BudgetDecision(
        tier=depth,
        thinking=thinking,
        reasoning_effort=effort,
        max_tokens=max_tokens,
        source=frame.source,
        confidence=frame.confidence,
        reason=reason,
    )


__all__ = [
    "CACHE_MAXSIZE",
    "DEFAULT_DEPTH",
    "DEFAULT_FRAME_TIMEOUT",
    "DecisionFrame",
    "DepthState",
    "DEPTHS",
    "FRAME_CONFIDENCE_THRESHOLD",
    "INTENTS",
    "RISKS",
    "SHORT_TEXT_CHARS",
    "SOURCES",
    "TONES",
    "clear_cache",
    "decide_by_rules_frame",
    "decide_frame",
    "frame_to_budget",
]
