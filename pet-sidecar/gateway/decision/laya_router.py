"""laya_router.py - Laya System 1 budget router with a deterministic fallback.

The Laya sidecar (``POST /v1/systemone``) classifies a user message into one
of four budgets: fast / normal / deep / max. Its zero-shot exact accuracy is
only 58-67%, so this module never trusts it blindly:

  * empty / very short messages short-circuit to fast (source=short);
  * successful, confident answers live in a 64-entry LRU keyed by a hash of
    the normalized text (plus the tool/emotion dimensions that affect the
    final budget);
  * a 400 ms timeout (configurable), transport errors, non-200 responses,
    malformed payloads and low-confidence answers all fall back to rules;
  * a confident answer is still mapped through :func:`map_tier`, so tool use
    and a clearly low mood can only *reduce* spend, never inflate it;
  * a deterministic *safety floor* runs after Laya answers: when rules see a
    deep/max signal (SQL tuning, architecture, "越详细越好") and Laya picked
    fast/normal, the rules tier wins while Laya's confidence/source stay.
    The floor only raises spend; a deep/max Laya answer is never downgraded,
    and explicit greetings are still capped back to fast.

Nothing connects at import time. ``decide_budget`` opens a short-lived
``httpx.AsyncClient`` per uncached call unless the caller injects a client or
a transport. Every network wait is bounded and asynchronous.

Environment:
    PET_LAYA_URL       sidecar base URL (default http://127.0.0.1:8765)
    PET_LAYA_TIMEOUT   HTTP budget in seconds (default 0.4)
    PET_LAYA_ENABLED   "0"/"false"/"no"/"off" disables routing (default on)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Tuple

import httpx

from .meta import decide_thinking

logger = logging.getLogger(__name__)

DEFAULT_LAYA_URL = "http://127.0.0.1:8765"
# Spike HTTP p50 was 206-311 ms; 400 ms keeps the router off the critical
# path while giving the tail room before the deterministic rules fallback.
DEFAULT_LAYA_TIMEOUT = 0.4
DEFAULT_LAYA_ENABLED = True

LAYA_URL_ENV = "PET_LAYA_URL"
LAYA_TIMEOUT_ENV = "PET_LAYA_TIMEOUT"
LAYA_ENABLED_ENV = "PET_LAYA_ENABLED"

LAYA_MODEL = "zh-tier"
LAYA_ENDPOINT = "/v1/systemone"

# Four-way exact accuracy is only 58-67%; below this the sidecar is guessing.
CONFIDENCE_THRESHOLD = 0.45

CACHE_MAXSIZE = 64
SHORT_TEXT_CHARS = 4
LONG_TEXT_CHARS = 200

LOW_MOOD_THRESHOLD = -0.4
# One notch below the 4096 max ceiling: a clearly low mood may still need a
# deep answer, but it does not need the most expensive one.
LOW_MOOD_TOKEN_CEILING = 2048

TOOL_TOKEN_FLOOR = 1024

TIERS = ("fast", "normal", "deep", "max")

# tier -> (thinking, reasoning_effort, max_tokens)
_TIER_SPEC: Dict[str, Tuple[bool, str, int]] = {
    "fast": (False, "low", 512),
    "normal": (False, "low", 1024),
    "deep": (True, "high", 2048),
    "max": (True, "max", 4096),
}

# Reused as a coarse signal by the rules path. These overlap meta.DEEP_HINTS
# on purpose; the extra entries cover code/error vocabulary that shows up in
# the gateway's real traffic.
_CODE_HINTS = (
    "```",
    "def ",
    "class ",
    "import ",
    "traceback",
    "keyerror",
    "typeerror",
    "报错",
    "错误",
    "异常",
    "堆栈",
    "代码",
    "函数",
    "接口",
    "数据库",
    "bug",
    "debug",
    "修复",
    "sql",
    "索引",
    "慢查询",
    "性能分析",
    "性能调优",
    "调优",
    "error",
    "exception",
    "stack trace",
    "syntax",
    "npm ",
    "pip ",
    "docker",
    "git ",
    "console.log",
    "<html",
    "select ",
    "curl ",
)

_MAX_HINTS = (
    "系统设计",
    "架构设计",
    "详细设计",
    "完整方案",
    "完整架构",
    "架构方案",
    "深入分析",
    "深度分析",
    "详细论证",
    "尽可能详细",
    "越详细越好",
    "最大预算",
    "长篇",
    "论文",
    "白皮书",
    "完整实现",
    "从零实现",
    "从零设计",
    "分布式",
    "微服务",
    "千万级",
    "高并发",
    "全流程",
    "端到端",
)

# Architecture-shaped vocabulary. A max hint on its own is not enough for the
# top tier (a long chat can say "详细说说"); pairing it with one of these
# words, or with enough length, is what marks a real system-design task.
_ARCH_HINTS = (
    "架构",
    "系统设计",
    "分布式",
    "微服务",
    "千万级",
    "高并发",
    "从零设计",
    "数据结构",
    "容错",
    "降级",
    "限流",
    "方案",
)

_ACTION_HINTS = (
    "打开",
    "搜索",
    "查一下",
    "查查",
    "帮我",
    "截图",
    "运行",
    "执行",
    "发送",
    "提醒",
    "设置",
    "安装",
    "下载",
    "关闭",
)

_GREETINGS = (
    "你好",
    "您好",
    "嗨",
    "哈喽",
    "早上好",
    "早安",
    "中午好",
    "下午好",
    "晚上好",
    "晚安",
    "在吗",
    "在不在",
    "谢谢",
    "多谢",
    "好的",
    "好呀",
    "嗯",
    "哦",
    "ok",
    "okay",
    "hi",
    "hello",
    "hey",
    "thanks",
    "bye",
    "再见",
    "拜拜",
)

_PUNCT_RE = re.compile(r"[\s，,。.!！?？~～…]+")


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """One routing decision. ``confidence`` is only probabilistic for Laya."""

    tier: str
    thinking: bool
    reasoning_effort: str
    max_tokens: int
    source: str
    confidence: float = 0.0
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort,
            "max_tokens": self.max_tokens,
            "source": self.source,
            "confidence": self.confidence,
            "reason": self.reason,
        }


# Key: (sha256 of normalized text, has_tools, coarse emotion bucket).
_CACHE: "OrderedDict[Tuple[str, bool, int], BudgetDecision]" = OrderedDict()


def clear_cache() -> None:
    """Drop cached Laya answers (tests and config reloads)."""
    _CACHE.clear()


def _clean_text(text: Any) -> str:
    """Preserve internal whitespace/newlines; only trim the ends."""
    if text is None:
        return ""
    return str(text).strip()


def _cache_text(text: str) -> str:
    """Normalize for hashing: whitespace collapsed, case-folded."""
    return " ".join(text.casefold().split())


def _digest(text: str) -> str:
    return hashlib.sha256(_cache_text(text).encode("utf-8")).hexdigest()


def _coerce_emotion(value: Any) -> float:
    try:
        emotion = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(emotion):
        return 0.0
    return max(-1.0, min(1.0, emotion))


def _emotion_bucket(emotion: float) -> int:
    # Excitement intentionally maps like neutral; only low mood changes the
    # budget today, so only that boundary splits the cache.
    return -1 if emotion < LOW_MOOD_THRESHOLD else 0


def _env_url() -> str:
    raw = (os.environ.get(LAYA_URL_ENV) or "").strip()
    url = (raw or DEFAULT_LAYA_URL).rstrip("/")
    return url or DEFAULT_LAYA_URL


def _env_timeout() -> float:
    raw = (os.environ.get(LAYA_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_LAYA_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_LAYA_TIMEOUT
    return value if math.isfinite(value) and value > 0 else DEFAULT_LAYA_TIMEOUT


def _laya_enabled() -> bool:
    raw = (os.environ.get(LAYA_ENABLED_ENV) or "").strip().lower()
    if not raw:
        return DEFAULT_LAYA_ENABLED
    return raw not in {"0", "false", "no", "off", "disabled"}


def _resolve_timeout(timeout: Optional[float]) -> float:
    if timeout is None:
        return _env_timeout()
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return _env_timeout()
    return value if math.isfinite(value) and value > 0 else _env_timeout()


def _endpoint_url() -> str:
    return f"{_env_url()}{LAYA_ENDPOINT}"


def _build_payload(text: str) -> Dict[str, Any]:
    return {
        "model": LAYA_MODEL,
        "state": text,
        "questions": {
            "thinking": {
                "type": "choice",
                "instructions": (
                    "这条用户消息需要多少思考预算？"
                    "按回答它所需的工作量选择一档。"
                ),
                "criteria": {
                    "fast": "打招呼、寒暄、确认、极短的一句话，不需要推理",
                    "normal": "普通聊天、简单事实问题、日常建议，一两句话就能回答",
                    "deep": "写代码、排查报错、分析推理、多步任务，需要认真思考",
                    "max": "明确要求深入或长篇，或系统设计、复杂架构，需要最大思考预算",
                },
            }
        },
    }


def map_tier(
    tier: str,
    has_tools: bool = False,
    emotion_index: float = 0.0,
) -> BudgetDecision:
    """Pure tier -> budget mapping.

    * fast   -> thinking off, low effort, 512 tokens
    * normal -> thinking off, low effort, 1024 tokens
    * deep   -> thinking on, high effort, 2048 tokens
    * max    -> thinking on, max effort, 4096 tokens

    Tool availability lifts fast/normal to at least 1024 tokens. A clearly
    low mood demotes max to deep and caps the budget at 2048 tokens.
    Excitement deliberately does *not* promote normal to deep: the sidecar
    is only 58-67% exact and extra thinking on a happy short message costs
    latency without improving the answer. Keeping the mapping legible beats
    a speculative mood bonus.
    """
    key = str(tier or "").strip().lower()
    if key not in _TIER_SPEC:
        raise ValueError(f"unknown tier: {tier!r}")

    thinking, effort, max_tokens = _TIER_SPEC[key]

    if bool(has_tools) and key in ("fast", "normal"):
        max_tokens = max(max_tokens, TOOL_TOKEN_FLOOR)

    emotion = _coerce_emotion(emotion_index)
    if emotion < LOW_MOOD_THRESHOLD:
        if key == "max":
            key = "deep"
            thinking, effort, max_tokens = _TIER_SPEC[key]
        max_tokens = min(max_tokens, LOW_MOOD_TOKEN_CEILING)

    return BudgetDecision(
        tier=key,
        thinking=thinking,
        reasoning_effort=effort,
        max_tokens=max_tokens,
        source="rules",
    )


def _compact(text: str) -> str:
    return _PUNCT_RE.sub("", text.casefold())


def _is_greeting(text: str) -> bool:
    """True only for a greeting/ack with essentially nothing else attached.

    "你好，帮我看看这段代码" must not become fast just because it starts
    with a greeting; the remainder has to be tiny. ASCII greetings use a
    word boundary so "history" / "high" / "thanksgiving" do not match.
    """
    stripped = text.strip()
    compact = _compact(stripped)
    if not compact or len(compact) > 20:
        return False

    for greeting in _GREETINGS:
        if greeting.isascii():
            match = re.match(
                rf"^{re.escape(greeting)}\b",
                stripped,
                re.IGNORECASE,
            )
            if match is None:
                continue
            remainder = _compact(stripped[match.end():])
        else:
            if not compact.startswith(greeting):
                continue
            remainder = compact[len(greeting):]
        if len(remainder) <= 6:
            return True
    return False


def _has_code_signal(low: str) -> bool:
    return any(hint in low for hint in _CODE_HINTS)


def _has_max_signal(low: str) -> bool:
    return any(hint in low for hint in _MAX_HINTS)


def _has_arch_signal(low: str) -> bool:
    return any(hint in low for hint in _ARCH_HINTS)


def _has_action_signal(low: str) -> bool:
    return any(hint in low for hint in _ACTION_HINTS)


def _rule_tier(text: str, has_tools: bool) -> Tuple[str, str]:
    """Deterministic tier + signal name. Order is precedence order."""
    if len(text) < SHORT_TEXT_CHARS:
        return "fast", "short"
    if _is_greeting(text):
        return "fast", "greeting"

    low = text.casefold()
    code = _has_code_signal(low)
    wants_max = _has_max_signal(low)

    # Top tier needs a max hint *and* an architecture shape or real length.
    # A bare "详细说说这条 SQL" stays deep: detail words alone are cheap.
    if wants_max and (_has_arch_signal(low) or len(text) >= 24):
        return "max", "max_hint"
    if code:
        # SQL / index / profiling vocabulary is deep work, not max work,
        # unless it also carried a max + architecture signal above.
        return "deep", "code"
    if len(text) >= LONG_TEXT_CHARS:
        return "deep", "long"

    # A short factual question ("今天天气怎么样？", "你叫什么名字？") is not
    # deep work just because it contains 怎么/为什么; the meta seat is
    # intentionally broad and must not promote these.
    if (
        len(text) <= 16
        and text.rstrip().endswith(("?", "？"))
        and not code
        and not wants_max
    ):
        return "normal", "simple_question"

    # Reuse the existing meta-intuition seat for broad reasoning hints
    # (why/how/design/analysis/80+ chars/question mark).
    thought = decide_thinking({"last_user_msg": text})
    if thought.get("thinking"):
        reasons = "+".join(thought.get("reasons") or ["thinking"])
        return "deep", f"meta:{reasons}"

    # Having tools available is not itself a reason to think harder, but it
    # is a useful label; the token floor is applied by map_tier().
    if has_tools and _has_action_signal(low):
        return "normal", "tools"
    return "normal", "default"


def decide_by_rules(
    user_text: str,
    emotion_index: float = 0.0,
    has_tools: bool = False,
) -> BudgetDecision:
    """Deterministic fallback used for short/disabled/error/low-confidence."""
    text = _clean_text(user_text)
    tier, signal = _rule_tier(text, bool(has_tools))
    mapped = map_tier(tier, has_tools=has_tools, emotion_index=emotion_index)
    reason = f"rules:{signal}"
    if mapped.tier != tier:
        reason = f"{reason}->{mapped.tier}"
    return replace(mapped, confidence=0.0, reason=reason)


_TIER_RANK = {tier: rank for rank, tier in enumerate(TIERS)}


_FLOOR_SIGNALS = ("code", "max_hint", "long")


def _is_floor_signal(signal: str) -> bool:
    """Only strong, deterministic signals may raise Laya's budget.

    ``meta:deep_hint+question`` is deliberately not here: a short
    "今天天气怎么样？" contains 怎么 and would otherwise be floored to deep.
    """
    return signal in _FLOOR_SIGNALS or signal.startswith("code:") or "long" in signal


def _apply_floor(
    text: str,
    emotion: float,
    has_tools: bool,
    decision: BudgetDecision,
) -> BudgetDecision:
    """Keep Laya from under-spending on a task the rules clearly see.

    The rules only act as a *floor*: they may promote fast/normal to
    deep/max (and deep to max) when their deterministic signals are strong,
    never demote. Laya's source/confidence survive so telemetry still shows
    which component produced the answer, and ``reason`` records the signal.

    Greetings are the one hard cap: "早上好呀" answered with a deep tier is
    a sidecar misfire, so an explicit greeting goes back to fast.
    """
    rules = decide_by_rules(text, emotion_index=emotion, has_tools=has_tools)
    signal = (
        rules.reason[len("rules:"):]
        if rules.reason.startswith("rules:")
        else rules.reason
    )

    if signal == "greeting" and decision.tier != "fast":
        mapped = map_tier("fast", has_tools=has_tools, emotion_index=emotion)
        return replace(
            decision,
            tier=mapped.tier,
            thinking=mapped.thinking,
            reasoning_effort=mapped.reasoning_effort,
            max_tokens=mapped.max_tokens,
            reason=f"{decision.reason}|cap:greeting",
        )

    if (
        _TIER_RANK[rules.tier] > _TIER_RANK[decision.tier]
        and _TIER_RANK[rules.tier] >= _TIER_RANK["deep"]
        and _is_floor_signal(signal)
    ):
        return replace(
            decision,
            tier=rules.tier,
            thinking=rules.thinking,
            reasoning_effort=rules.reasoning_effort,
            max_tokens=rules.max_tokens,
            reason=f"{decision.reason}|floor:{signal}",
        )
    return decision


def _selected_confidence(thinking: Dict[str, Any], choice: str) -> Optional[float]:
    raw = thinking.get("answer_confidence")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        # answer_confidence is the selected-option probability. If an older
        # sidecar omits it, probabilities[choice] means the same thing.
        probabilities = thinking.get("probabilities")
        raw = probabilities.get(choice) if isinstance(probabilities, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    confidence = float(raw)
    if not math.isfinite(confidence):
        return None
    return max(0.0, min(1.0, confidence))


def _parse_choice(data: Any) -> "Tuple[str, float] | str":
    """Return (choice, confidence) or a short failure reason string."""
    if not isinstance(data, dict):
        return "invalid_payload"
    answers = data.get("answers")
    if not isinstance(answers, dict):
        return "invalid_payload"
    thinking = answers.get("thinking")
    if not isinstance(thinking, dict):
        return "invalid_payload"

    choice = thinking.get("choice")
    if not isinstance(choice, str):
        return "invalid_choice"
    choice = choice.strip().lower()
    if choice not in _TIER_SPEC:
        return f"invalid_choice:{choice[:24]}"

    confidence = _selected_confidence(thinking, choice)
    if confidence is None:
        return "invalid_confidence"
    return choice, confidence


async def _ask_laya(
    client: httpx.AsyncClient,
    text: str,
    timeout: float,
    has_tools: bool,
    emotion: float,
) -> "BudgetDecision | str":
    """One bounded attempt. Returns a BudgetDecision or a failure reason."""
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

        parsed = _parse_choice(data)
        if isinstance(parsed, str):
            return f"laya_{parsed}"

        choice, confidence = parsed
        if confidence < CONFIDENCE_THRESHOLD:
            return f"laya_low_confidence:{choice}@{confidence:.2f}"

        mapped = map_tier(choice, has_tools=has_tools, emotion_index=emotion)
        reason = f"laya:{choice}@{confidence:.2f}"
        if mapped.tier != choice:
            reason = f"{reason}->{mapped.tier}"
        return replace(
            mapped,
            source="laya",
            confidence=confidence,
            reason=reason,
        )
    finally:
        try:
            await response.aclose()
        except Exception:
            pass


def _fallback(
    text: str,
    emotion: float,
    has_tools: bool,
    failure: str,
) -> BudgetDecision:
    rules = decide_by_rules(text, emotion_index=emotion, has_tools=has_tools)
    logger.debug("laya router fallback (%s): %s", failure, rules.reason)
    return replace(rules, reason=f"{failure}|{rules.reason}")


def _cache_get(key: Tuple[str, bool, int]) -> Optional[BudgetDecision]:
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
    return hit


def _cache_put(key: Tuple[str, bool, int], decision: BudgetDecision) -> None:
    _CACHE[key] = decision
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_MAXSIZE:
        _CACHE.popitem(last=False)


async def decide_budget(
    user_text: str,
    emotion_index: float = 0.0,
    has_tools: bool = False,
    *,
    client: Optional[httpx.AsyncClient] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    timeout: Optional[float] = None,
) -> BudgetDecision:
    """Pick a budget for one user message.

    Args:
        user_text: raw user message.
        emotion_index: -1..1 mood signal; only a clearly low mood modulates.
        has_tools: whether tools are advertised for this turn.
        client: optional caller-owned AsyncClient (not closed here). When
            supplied, ``transport`` and the client timeout are ignored for
            construction, but the per-request ``timeout`` still applies.
        transport: optional httpx transport (e.g. MockTransport) for the
            short-lived client this module creates.
        timeout: override PET_LAYA_TIMEOUT for this call.

    Returns:
        A BudgetDecision whose ``source`` is one of laya/rules/short/cache.
    """
    text = _clean_text(user_text)
    has_tools = bool(has_tools)
    emotion = _coerce_emotion(emotion_index)

    if len(text) < SHORT_TEXT_CHARS:
        mapped = map_tier("fast", has_tools=has_tools, emotion_index=emotion)
        return replace(
            mapped,
            source="short",
            confidence=1.0,
            reason="short_text",
        )

    if not _laya_enabled():
        return _fallback(text, emotion, has_tools, "laya_disabled")

    key = (_digest(text), has_tools, _emotion_bucket(emotion))
    cached = _cache_get(key)
    if cached is not None:
        return replace(cached, source="cache", reason=f"{cached.reason}|cache")

    budget = _resolve_timeout(timeout)
    try:
        if client is not None:
            outcome = await _ask_laya(client, text, budget, has_tools, emotion)
        else:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(budget),
                transport=transport,
                trust_env=False,  # never let a proxy intercept loopback
            ) as http:
                outcome = await _ask_laya(http, text, budget, has_tools, emotion)
    except Exception as exc:
        # Last-resort guard: a router bug must never take chat down.
        logger.debug("laya router unexpected error", exc_info=True)
        outcome = f"laya_error:{type(exc).__name__}"

    if isinstance(outcome, str):
        return _fallback(text, emotion, has_tools, outcome)

    # Last line of defence against a confident-but-wrong fast/normal answer.
    # Runs before caching so cache hits return the already-floored decision.
    outcome = _apply_floor(text, emotion, has_tools, outcome)

    _cache_put(key, outcome)
    return outcome


__all__ = [
    "BudgetDecision",
    "CONFIDENCE_THRESHOLD",
    "DEFAULT_LAYA_TIMEOUT",
    "DEFAULT_LAYA_URL",
    "map_tier",
    "decide_by_rules",
    "decide_budget",
    "clear_cache",
]
