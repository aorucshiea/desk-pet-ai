"""Second-chance memory extraction when a reply omitted its MEM block.

The normal extraction hook remains the primary path.  This module performs one
small, bare-model retry in the background and feeds its output back through the
same synchronous extraction function, so validation and de-duplication are not
duplicated here.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Callable, Dict

from .. import telemetry
from .events import parse_event_block

_MEM_BLOCK_RE = re.compile(r"<<<MEM>>>\s*[\s\S]*?\s*<<<MEMEND>>>", re.IGNORECASE)

# Local engines are usually single-slot: a background extraction competes
# with the next user turn and makes the pet feel slow. Off by default there;
# a user who wants it can set PET_EXTRACT_FALLBACK_LOCAL=1.
LOCAL_FALLBACK_ENV = "PET_EXTRACT_FALLBACK_LOCAL"


def _is_local_provider(provider: Any) -> bool:
    if getattr(provider, "is_local", False):
        return True
    return str(getattr(provider, "name", "") or "").lower() == "local"


def should_fallback(user_text: str, reply_text: str) -> bool:
    """Whether this real, non-trivial user turn needs a second extraction."""
    user = str(user_text or "").strip()
    reply = str(reply_text or "")
    if _MEM_BLOCK_RE.search(reply):
        return False
    if not user or user.startswith("[系统提示") or len(user) < 8:
        return False
    return True


def build_prompt(
    user_text: str,
    reply_text: str,
    *,
    max_user_chars: int = 4000,
    max_reply_chars: int = 6000,
) -> str:
    """Build the bare-model retry prompt; the reply must be JSON only.

    The caps are deliberately much smaller than a full turn: extraction needs
    the gist, not the whole transcript. This keeps the fallback cheap on a
    cloud provider and short enough to prefill on a local one.
    """
    return (
        "请从下面这一轮对话中提取值得长期记住的内容。只输出一个 MEM JSON 对象，"
        "不要 Markdown，不要解释，不要复述对话。\n"
        "格式：{\"events\":[{\"title\":\"不超过15字\",\"content\":\"发生的事与感受\","
        "\"weight\":1-999,\"type\":\"experience或knowledge\",\"resolved\":true}],"
        "\"core\":[\"极少量的核心事件标题\"],"
        "\"mood\":{\"mood\":\"心情\",\"intensity\":0-100,\"reason\":\"原因\",\"changed\":true}}\n"
        "events 最多 3 条；没有值得记住的事就严格输出 {\"events\":[]}。"
        "core 和 mood 没有内容时可省略。不要编造对话里没有的事实。\n\n"
        "用户：\n" + str(user_text or "")[:max_user_chars] +
        "\n\n桌宠回复：\n" + str(reply_text or "")[:max_reply_chars]
    )


def _wrap_mem(text: str) -> str:
    raw = str(text or "").strip()
    if _MEM_BLOCK_RE.search(raw):
        return raw
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
    if fenced:
        raw = fenced.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    return f"<<<MEM>>>{raw}<<<MEMEND>>>"


async def run_fallback(
    provider,
    user_text: str,
    reply_text: str,
    apply_extraction: Callable[[str], dict],
    *,
    timeout: float = 90,
) -> Dict[str, Any]:
    """Run one guarded retry.  No provider/apply exception escapes to SSE."""
    result: Dict[str, Any] = {"attempted": False, "applied": False, "reason": "not_needed"}
    if not should_fallback(user_text, reply_text):
        return result

    local = _is_local_provider(provider)
    if local and os.environ.get(LOCAL_FALLBACK_ENV, "0") != "1":
        # Single-slot local engines: a second generation right after a turn
        # delays the user's next message. Best-effort memory is not worth
        # making the pet feel stuck. Opt in with PET_EXTRACT_FALLBACK_LOCAL=1.
        result["reason"] = "local_provider"
        telemetry.inc("memory.extraction.fallback_skipped_local")
        return result

    result["attempted"] = True
    result["reason"] = "pending"
    telemetry.inc("memory.extraction.fallback_attempts")
    max_user_chars, max_reply_chars = (2000, 3000) if local else (4000, 6000)

    async def _generate() -> str:
        chunks = []
        async for event in provider.chat(
            messages=[{"role": "user", "content": build_prompt(
                user_text,
                reply_text,
                max_user_chars=max_user_chars,
                max_reply_chars=max_reply_chars,
            )}],
            system="",
            tools=None,
            max_tokens=500,
            temperature=0.2,
            top_p=0.9,
        ):
            if event.get("type") == "delta":
                chunks.append(str(event.get("content") or ""))
            elif event.get("type") == "error":
                raise RuntimeError(event.get("message") or "provider error")
        return "".join(chunks)

    try:
        raw = await asyncio.wait_for(_generate(), timeout=timeout)
        wrapped = _wrap_mem(raw)
        if parse_event_block(wrapped) is None:
            result["reason"] = "unparseable_output"
            result["raw_len"] = len(raw)
            return result
        apply_result = apply_extraction(wrapped)
        result["applied"] = True
        result["reason"] = "ok"
        if isinstance(apply_result, dict):
            result["events_added"] = list(apply_result.get("events_added") or [])
            result["duplicate"] = bool(apply_result.get("duplicate", False))
        telemetry.inc("memory.extraction.fallback_applied")
    except asyncio.TimeoutError:
        result["reason"] = "timeout"
    except asyncio.CancelledError:
        result["reason"] = "cancelled"
        raise
    except Exception as exc:
        result["reason"] = "error"
        result["error"] = str(exc)[:300]
    return result
