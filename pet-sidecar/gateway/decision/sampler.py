"""sampler.py - Laya-assisted multi-option decision sampler (T1/T2).

The no-thinking chat path asks the LLM to propose 2-6 concrete options with
gut weights, then asks Laya (local System-1 sidecar) for probabilities over
those exact option ids.  This module combines both into one posterior:

    score[id] = prior[id] ** PRIOR_EXPONENT
                * (laya_prob[id] + 1e-9) ** LAYA_EXPONENT

The exponents are a compromise between "prior dominates" and "Laya leads".
With equal priors (0.85 vs 0.6) Laya clearly shapes the posterior instead of
the flat 32/29/25/14 we measured at 0.7/0.3; with a strong prior (.9 vs .1,
Laya reversed) the prior still wins ~63/37.

Sampling happens over a top-3 shortlist, not the whole option set: the
posterior is sorted descending (ties keep option order), the best
``SHORTLIST_SIZE`` survive, and only they are rolled with their posterior
weights.  A tail option can therefore never win by luck, while the actual
pick inside the shortlist stays whimsical.  ``confidence`` is deliberately
NOT renormalized — it stays the posterior probability over ALL options so
callers keep a calibrated confidence signal.

A seeded ``random.Random`` makes identical input+options roll identically;
the caller passes a fresh seed for a fresh roll.  Every roll and every
human override is appended to a JSONL log for later distillation.

Nothing connects at import time.  ``roll_decision`` opens a short-lived
``httpx.AsyncClient`` per call unless a client or transport is injected
(tests use ``httpx.MockTransport``).  Transport, HTTP and JSON failures
never raise; they return a prior-only result with a short error reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import httpx

from .laya_router import LAYA_MODEL, _clean_text, _endpoint_url, _laya_enabled

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 0.45
MAX_OPTIONS = 6
MIN_OPTIONS = 2
PRIOR_EXPONENT = 0.85
LAYA_EXPONENT = 0.6
# Rolls are drawn from the top-N options only: a 3% tail option must never
# win a roll just because the dice felt whimsical.
SHORTLIST_SIZE = 3

ROLL_TIMEOUT_ENV = "PET_LAYA_ROLL_TIMEOUT"
LAYA_TIMEOUT_ENV = "PET_LAYA_TIMEOUT"
DECISION_LOG_ENV = "PET_DECISION_LOG"

# Tiny identity header only: the full persona would drown the sidecar.
_IDENTITY = "你是桌宠的潜意识直觉层，只做快速选择，不解释，不写回答。"
_QUESTION_MAX_CHARS = 300
_LABEL_MAX_CHARS = 80
_USER_TEXT_MAX_CHARS = 500
_CONTEXT_MAX_CHARS = 300
_STATE_MAX_CHARS = 900

_DEFAULT_WEIGHT = 1.0
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")

_LOG_MAX_BYTES = 4 * 1024 * 1024
_RECENT_MAXSIZE = 64

_ROLLS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_RECENT_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_question(question: Any) -> str:
    text = _clean_text(question)[: _QUESTION_MAX_CHARS]
    if not text:
        raise ValueError("question must be a non-empty string (max 300 chars)")
    return text


def _validate_options(options: Any) -> List[Dict[str, Any]]:
    if not isinstance(options, (list, tuple)):
        raise ValueError("options must be a list of 2..6 dicts")
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise ValueError(
            f"options must contain {MIN_OPTIONS}..{MAX_OPTIONS} items "
            f"(got {len(options)})"
        )

    cleaned: List[Dict[str, Any]] = []
    seen = set()
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            raise ValueError(f"options[{index}] must be a dict with id/label/weight")

        option_id = option.get("id")
        if not isinstance(option_id, str) or not _ID_RE.fullmatch(option_id):
            raise ValueError(
                f"options[{index}].id must match ^[A-Za-z0-9_-]{{1,24}}$ "
                f"(got {option_id!r})"
            )
        if option_id in seen:
            raise ValueError(f"duplicate option id: {option_id!r}")
        seen.add(option_id)

        label = _clean_text(option.get("label"))[: _LABEL_MAX_CHARS]
        if not label:
            raise ValueError(f"options[{index}].label must be a non-empty string")

        weight = option.get("weight", _DEFAULT_WEIGHT)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ValueError(
                f"options[{index}].weight must be a number in [0, 1] "
                f"(got {weight!r})"
            )
        value = float(weight)
        if not math.isfinite(value):
            raise ValueError(
                f"options[{index}].weight must be finite (got {weight!r})"
            )

        cleaned.append(
            {
                "id": option_id,
                "label": label,
                "weight": max(0.0, min(1.0, value)),
            }
        )
    return cleaned


# ---------------------------------------------------------------------------
# Prior / state / payload
# ---------------------------------------------------------------------------


def _prior_from_options(options: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    weights = [option["weight"] for option in options]
    total = sum(weights)
    if total <= 0.0:
        # All-zero weights are "no opinion", not "never pick anything".
        share = 1.0 / len(options)
        return {option["id"]: share for option in options}
    return {option["id"]: option["weight"] / total for option in options}


def _build_state(user_text: str, context: str) -> str:
    text = _clean_text(user_text)
    ctx = _clean_text(context)
    lines = [
        _IDENTITY,
        f"用户刚才说：{text[: _USER_TEXT_MAX_CHARS]}",
    ]
    if ctx:
        lines.append(f"情境：{ctx[: _CONTEXT_MAX_CHARS]}")
    return "\n".join(lines)[: _STATE_MAX_CHARS]


def _build_payload(
    state: str,
    question: str,
    options: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "model": LAYA_MODEL,
        "state": state,
        "questions": {
            "roll": {
                "type": "choice",
                "instructions": f"凭直觉选一个最合适的选项：{question}",
                "criteria": {option["id"]: option["label"] for option in options},
            }
        },
    }


def _env_timeout() -> Optional[float]:
    for name in (ROLL_TIMEOUT_ENV, LAYA_TIMEOUT_ENV):
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if math.isfinite(value) and value > 0.0:
            return value
    return None


def _resolve_timeout(timeout: Optional[float]) -> float:
    if timeout is not None:
        try:
            value = float(timeout)
        except (TypeError, ValueError):
            value = None
        if value is not None and math.isfinite(value) and value > 0.0:
            return value
    env = _env_timeout()
    return env if env is not None else DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    return result


def _parse_probabilities(
    raw: Any,
    ids: Sequence[str],
) -> Optional[Dict[str, float]]:
    if not isinstance(raw, dict):
        return None
    values: Dict[str, float] = {}
    for option_id in ids:
        if option_id not in raw:
            values[option_id] = 0.0
            continue
        value = _number(raw[option_id])
        if value is None:
            return None
        values[option_id] = max(0.0, value)
    total = sum(values.values())
    if total <= 0.0:
        return None
    return {option_id: values[option_id] / total for option_id in ids}


def _parse_answer(
    data: Any,
    ids: Sequence[str],
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return (laya_info, "") or (None, short_failure_reason)."""
    if not isinstance(data, dict):
        return None, "laya_invalid_payload"
    answers = data.get("answers")
    if not isinstance(answers, dict):
        return None, "laya_invalid_payload"
    roll = answers.get("roll")
    if not isinstance(roll, dict):
        return None, "laya_invalid_payload"

    raw_choice = roll.get("choice")
    choice: Optional[str] = None
    if raw_choice is not None:
        if not isinstance(raw_choice, str):
            return None, "laya_invalid_choice"
        stripped = raw_choice.strip()
        if stripped and stripped not in ids:
            return None, f"laya_invalid_choice:{stripped[:24]}"
        choice = stripped or None

    probabilities = _parse_probabilities(roll.get("probabilities"), ids)
    if probabilities is not None:
        if choice is None:
            # No explicit choice: trust the distribution's argmax. Ties keep
            # the option order because max() returns the first maximum.
            choice = max(ids, key=lambda option_id: probabilities[option_id])
        return (
            {
                "choice": choice,
                "probabilities": probabilities,
                "confidence": probabilities[choice],
                "source": "laya",
                "error": "",
            },
            "",
        )

    if choice is None:
        return None, "laya_invalid_probabilities"

    # Older/leaner sidecars can omit probabilities. Describe the selected
    # option as one-hot with the reported (or default 0.5) confidence and
    # spread the remainder evenly so the distribution still sums to 1.
    confidence = _number(roll.get("answer_confidence"))
    if confidence is None:
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    rest = (1.0 - confidence) / (len(ids) - 1)
    one_hot = {
        option_id: (confidence if option_id == choice else rest) for option_id in ids
    }
    return (
        {
            "choice": choice,
            "probabilities": one_hot,
            "confidence": confidence,
            "source": "laya",
            "error": "",
        },
        "",
    )


async def _ask_laya(
    client: httpx.AsyncClient,
    payload: Dict[str, Any],
    timeout: float,
    ids: Sequence[str],
) -> Tuple[Optional[Dict[str, Any]], str]:
    """One bounded POST. Returns (laya_info, "") or (None, failure)."""
    try:
        response = await asyncio.wait_for(
            client.post(
                _endpoint_url(),
                json=payload,
                timeout=httpx.Timeout(timeout),
            ),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return None, "laya_timeout"
    except Exception as exc:  # connect / proxy / protocol errors
        return None, f"laya_error:{type(exc).__name__}"

    try:
        if response.status_code != 200:
            return None, f"laya_http_{response.status_code}"
        try:
            data = response.json()
        except Exception as exc:
            return None, f"laya_invalid_json:{type(exc).__name__}"
        return _parse_answer(data, ids)
    finally:
        try:
            await response.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Posterior + sampling
# ---------------------------------------------------------------------------


def _posterior(
    prior: Dict[str, float],
    laya_probabilities: Dict[str, float],
) -> Dict[str, float]:
    scores = {}
    for option_id, prior_value in prior.items():
        laya_value = laya_probabilities.get(option_id, 0.0)
        scores[option_id] = (prior_value**PRIOR_EXPONENT) * (
            (laya_value + 1e-9) ** LAYA_EXPONENT
        )
    total = sum(scores.values())
    if total <= 0.0:
        return dict(prior)
    return {option_id: score / total for option_id, score in scores.items()}


def _shortlist(
    ids: Sequence[str],
    posterior: Dict[str, float],
) -> List[str]:
    """Top-N option ids by posterior, in posterior order.

    ``sorted`` is stable, so equal posteriors keep the caller's option order.
    """
    ranked = sorted(ids, key=lambda option_id: -posterior.get(option_id, 0.0))
    return ranked[: max(1, min(SHORTLIST_SIZE, len(ranked)))]


# ---------------------------------------------------------------------------
# JSONL persistence + in-process recent rolls
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _resolve_log_path(log_path: Optional[Any]) -> Path:
    if log_path is not None:
        return Path(log_path).expanduser()
    env = (os.environ.get(DECISION_LOG_ENV) or "").strip()
    if env:
        return Path(env).expanduser()
    return Path("~/.pet/decision-rolls.jsonl").expanduser()


def _append_record(record: Dict[str, Any], log_path: Optional[Any]) -> None:
    """Best-effort append; a logging failure must never break a roll."""
    path = _resolve_log_path(log_path)
    try:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        logger.debug("decision log serialization failed", exc_info=True)
        return

    with _LOG_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.exists() and path.stat().st_size > _LOG_MAX_BYTES:
                    path.replace(Path(str(path) + ".1"))
            except OSError:
                logger.debug("decision log rotation skipped", exc_info=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            logger.debug("decision log append failed", exc_info=True)


def _remember(record: Dict[str, Any]) -> None:
    roll_id = record.get("roll_id")
    if not roll_id:
        return
    with _RECENT_LOCK:
        _ROLLS[roll_id] = record
        _ROLLS.move_to_end(roll_id)
        while len(_ROLLS) > _RECENT_MAXSIZE:
            _ROLLS.popitem(last=False)


def _recent_get(roll_id: str) -> Optional[Dict[str, Any]]:
    with _RECENT_LOCK:
        record = _ROLLS.get(roll_id)
        if record is not None:
            _ROLLS.move_to_end(roll_id)
        return record


def clear_recent() -> None:
    """Drop the in-process roll LRU (tests / config reloads)."""
    with _RECENT_LOCK:
        _ROLLS.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def roll_decision(
    question: str,
    options: List[Dict[str, Any]],
    *,
    context: str = "",
    user_text: str = "",
    seed: Optional[int] = None,
    client: Optional[httpx.AsyncClient] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    timeout: Optional[float] = None,
    log_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Roll one decision over caller-supplied options.

    Args:
        question: the choice being made (trimmed, capped at 300 chars).
        options: 2..6 ``{"id", "label", "weight"}`` dicts. Ids are unique
            ASCII tokens; weights are clamped to [0, 1] (default 1.0).
        context: optional short situation note (first 300 chars reach Laya).
        user_text: current real user message (first 500 chars reach Laya).
        seed: ``random.Random`` seed. Defaults to a hash of state+question+ids
            so identical input rolls identically; pass a new seed to re-roll.
        client: caller-owned AsyncClient (not closed here). Wins over
            ``transport``.
        transport: httpx transport for the short-lived client this module
            creates (tests use MockTransport).
        timeout: explicit HTTP budget; otherwise PET_LAYA_ROLL_TIMEOUT, then
            PET_LAYA_TIMEOUT, then 0.45 s.
        log_path: JSONL path override; otherwise PET_DECISION_LOG, then
            ``~/.pet/decision-rolls.jsonl``.

    Returns:
        A dict with ok/roll_id/question/choice/confidence/shortlist/
        posterior/prior/laya/options/seed/source/latency_ms/reason. The
        sample is drawn from the top-3 posterior options only; confidence is
        the posterior over all options. Invalid *input* raises ValueError;
        Laya failures degrade to source="prior".
    """
    question_text = _validate_question(question)
    validated_options = _validate_options(options)
    ids = [option["id"] for option in validated_options]

    user_text_text = _clean_text(user_text)
    context_text = _clean_text(context)
    state = _build_state(user_text_text, context_text)
    prior = _prior_from_options(validated_options)

    if seed is None:
        digest = hashlib.sha256(
            (state + "\n" + question_text + "\n" + "|".join(ids)).encode("utf-8")
        ).hexdigest()
        seed_used: Any = int(digest[:8], 16)
    else:
        seed_used = seed

    started = time.perf_counter()
    answer: Optional[Dict[str, Any]] = None
    failure = ""
    if not _laya_enabled():
        failure = "laya_disabled"
    else:
        payload = _build_payload(state, question_text, validated_options)
        budget = _resolve_timeout(timeout)
        try:
            if client is not None:
                answer, failure = await _ask_laya(client, payload, budget, ids)
            else:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(budget),
                    transport=transport,
                    trust_env=False,  # never let a proxy intercept loopback
                ) as http:
                    answer, failure = await _ask_laya(http, payload, budget, ids)
        except Exception as exc:
            # Last-resort guard: a sampler bug must never take chat down.
            logger.debug("decision sampler unexpected error", exc_info=True)
            answer, failure = None, f"laya_error:{type(exc).__name__}"
    latency_ms = round((time.perf_counter() - started) * 1000.0, 1)

    if answer is None:
        laya_info: Dict[str, Any] = {
            "choice": None,
            "probabilities": {},
            "confidence": 0.0,
            "source": "error",
            "error": failure or "laya_unavailable",
        }
        posterior = dict(prior)
        source = "prior"
    else:
        laya_info = answer
        posterior = _posterior(prior, answer["probabilities"])
        source = "laya+prior"

    rng = random.Random(seed_used)
    shortlist = _shortlist(ids, posterior)
    choice = rng.choices(
        shortlist,
        weights=[posterior[option_id] for option_id in shortlist],
        k=1,
    )[0]
    # Confidence stays the posterior over ALL options (never renormalized to
    # the shortlist), so it remains a calibrated signal for the caller.
    confidence = posterior[choice]
    roll_id = "roll_" + secrets.token_hex(8)
    shortlist_text = ",".join(shortlist)

    if answer is not None:
        reason = (
            f"laya:{answer['choice']}@{answer['confidence']:.2f}"
            f"|prior:{prior[choice]:.2f}|sample|shortlist={shortlist_text}"
        )
    else:
        reason = (
            f"{laya_info['error']}|prior:{prior[choice]:.2f}"
            f"|sample|shortlist={shortlist_text}"
        )

    result = {
        "ok": True,
        "roll_id": roll_id,
        "question": question_text,
        "choice": choice,
        "confidence": confidence,
        "shortlist": shortlist,
        "posterior": posterior,
        "prior": prior,
        "laya": laya_info,
        "options": validated_options,
        "seed": seed_used,
        "source": source,
        "latency_ms": latency_ms,
        "reason": reason,
    }

    # Persist for later distillation. Error text is deliberately dropped
    # here: roll records stay compact, the return value keeps the detail.
    record = {
        "type": "roll",
        "ts": _now_iso(),
        "roll_id": roll_id,
        "question": question_text,
        "context": context_text,
        "user_text": user_text_text,
        "options": validated_options,
        "prior": prior,
        "posterior": posterior,
        "shortlist": shortlist,
        "choice": choice,
        "confidence": confidence,
        "laya": {
            "choice": laya_info["choice"],
            "probabilities": laya_info["probabilities"],
            "confidence": laya_info["confidence"],
            "source": laya_info["source"],
        },
        "seed": seed_used,
        "source": source,
        "latency_ms": latency_ms,
    }
    _remember(record)
    _append_record(record, log_path)
    return result


def record_override(
    roll_id: str,
    choice: str,
    reason: str,
    *,
    log_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Append a human override for a known (or unknown) roll.

    Unknown ids are still logged with ``known: false`` so a gateway restart
    does not silently lose the signal. An out-of-options choice is logged
    too, flagged with ``choice_not_in_options`` for the distiller.
    """
    roll_key = _clean_text(roll_id)
    choice_text = _clean_text(choice)
    reason_text = _clean_text(reason)
    if not roll_key:
        raise ValueError("roll_id must be a non-empty string")
    if not choice_text:
        raise ValueError("choice must be a non-empty string")
    if not reason_text:
        raise ValueError("reason must be a non-empty string")

    roll = _recent_get(roll_key)
    known = roll is not None
    entry: Dict[str, Any] = {
        "type": "override",
        "ts": _now_iso(),
        "roll_id": roll_key,
        "choice": choice_text,
        "reason": reason_text,
        "known": known,
        "roll": roll,
    }
    if known:
        option_ids = {
            option.get("id")
            for option in (roll.get("options") or [])
            if isinstance(option, dict)
        }
        if choice_text not in option_ids:
            entry["choice_not_in_options"] = True
    _append_record(entry, log_path)

    return {
        "ok": True,
        "roll_id": roll_key,
        "choice": choice_text,
        "reason": reason_text,
        "known": known,
        "roll": roll,
    }


__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_OPTIONS",
    "MIN_OPTIONS",
    "PRIOR_EXPONENT",
    "LAYA_EXPONENT",
    "SHORTLIST_SIZE",
    "roll_decision",
    "record_override",
    "clear_recent",
]
