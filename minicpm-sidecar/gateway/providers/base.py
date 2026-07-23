"""Abstract base provider and shared types for the model provider layer.

Each provider wraps one model backend (local llama-server, OpenAI-compatible
API, Anthropic API) and exposes a standard `chat()` generator that yields
ChatEvent dicts.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional

from ..log_setup import get_logger


# ── Shared types ────────────────────────────────────────────────────────


@dataclass
class ToolDef:
    """Definition of an MCP tool sent to the model provider."""
    server_name: str
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)


@dataclass
class ToolCall:
    """A tool call requested by the model."""
    id: str
    server_name: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class ToolResult:
    """Result of executing an MCP tool call."""
    tool_call_id: str
    content: list[dict] = field(default_factory=list)
    is_error: bool = False

    def to_text(self) -> str:
        parts = []
        for item in self.content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "resource":
                    parts.append(str(item.get("resource", "")))
            else:
                parts.append(str(item))
        return "\n".join(parts)


# Simplified format regex fallback when only one server is active
# Format: [MCP:tool_name:{json}]
MCP_CALL_SIMPLE_PATTERN = re.compile(
    r'\[MCP:([A-Za-z0-9_-]+):(\{.*?\})\]'
)

_MCP_MARKER = "[MCP:"
# Also accept [builtin/tool_name:{json}] without the MCP: prefix — some
# models (e.g. Qwen3.5) drop the "MCP:" prefix and output [builtin/...]
# directly. This marker catches that variant.
_BUILTIN_MARKER = "[builtin/"
# Some models output [skill:skill_name] instead of [MCP:builtin/tool_name].
# Map skill names to their corresponding builtin tool calls.
_SKILL_MARKER = "[skill:"
_SKILL_TO_TOOL = {
    "screen-observe": {"server_name": "builtin", "name": "observe_screen", "arguments": {}},
    "screen-click": {"server_name": "builtin", "name": "screen_click", "arguments": {}},
}

# Chinese full-width bracket variants — Qwen models frequently output
# 【MCP:...】 or 【builtin/...】 instead of [MCP:...] / [builtin/...].
# We normalise these before scanning.
_FULL_OPEN = "\u3010"   # 【
_FULL_CLOSE = "\u3011"  # 】


def _normalise_brackets(text: str) -> str:
    """Replace Chinese full-width brackets 【】 with ASCII [] so the
    rest of the parser only needs to handle one bracket style."""
    return text.replace(_FULL_OPEN, "[").replace(_FULL_CLOSE, "]")


def _find_balanced_json(text: str, start: int) -> tuple[str, int, int] | None:
    r"""Extract a brace-balanced JSON object starting at position *start*.

    Returns (json_str, json_start, json_end) where json_start is the index of
    the opening '{' and json_end is the index just past the matching '}'.
    Returns None if no balanced object is found.
    """
    brace_start = text.find("{", start)
    if brace_start < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(brace_start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (text[brace_start:i + 1], brace_start, i + 1)

    return None


def parse_mcp_calls(text: str) -> list[dict]:
    """Extract MCP tool calls from model output text.

    Uses brace-balanced scanning to handle nested JSON objects.
    Falls back to simplified regex for non-nested tool calls.

    Returns list of dicts with keys: server_name, name, arguments.
    """
    # Normalise Chinese full-width brackets 【】 → [] before scanning
    text = _normalise_brackets(text)
    results: list[dict] = []
    used_ranges: list[tuple[int, int]] = []
    idx = 0

    while True:
        # Search for [MCP:...], [builtin/...], and [skill:...] markers
        mcp_pos = text.find(_MCP_MARKER, idx)
        builtin_pos = text.find(_BUILTIN_MARKER, idx)
        skill_pos = text.find(_SKILL_MARKER, idx)

        # Pick whichever comes first (and exists)
        candidates = []
        if mcp_pos >= 0:
            candidates.append((mcp_pos, "mcp"))
        if builtin_pos >= 0:
            candidates.append((builtin_pos, "builtin"))
        if skill_pos >= 0:
            candidates.append((skill_pos, "skill"))
        if not candidates:
            break
        candidates.sort()
        marker_pos, marker_type = candidates[0]

        if marker_type == "skill":
            # Format: [skill:skill_name] — map to builtin tool call
            after_marker = marker_pos + len(_SKILL_MARKER)
            close_pos = text.find("]", after_marker)
            if close_pos > after_marker:
                skill_name = text[after_marker:close_pos].strip()
                if skill_name in _SKILL_TO_TOOL:
                    results.append(dict(_SKILL_TO_TOOL[skill_name]))
                    used_ranges.append((marker_pos, close_pos + 1))
                    idx = close_pos + 1
                    continue
            idx = after_marker
            continue

        is_builtin = (marker_type == "builtin")

        if is_builtin:
            # Format: [builtin/tool_name:{json}]  (server_name is always "builtin")
            after_marker = marker_pos + len(_BUILTIN_MARKER)
            # after_marker points to just after "[builtin/"
            # Find the closing bracket to get tool_name
            close_pos = text.find("]", after_marker)
            colon_pos = text.find(":", after_marker)
            if colon_pos >= 0 and (close_pos < 0 or colon_pos < close_pos):
                # Has colon — [builtin/tool_name:{json}]
                prefix = text[after_marker:colon_pos]  # tool_name
                balanced = _find_balanced_json(text, colon_pos + 1)
                if balanced is None:
                    idx = colon_pos + 1
                    continue
                json_str, _, json_end = balanced
                try:
                    args = json.loads(json_str)
                except json.JSONDecodeError:
                    args = {}
                server_name = "builtin"
                name = prefix.strip()
                if name:
                    results.append({"server_name": server_name, "name": name, "arguments": args})
                    used_ranges.append((marker_pos, json_end + 1))  # +1 for ']'
                    idx = text.find("]", json_end) + 1 if text.find("]", json_end) >= 0 else json_end
                    continue
                idx = colon_pos + 1
                continue
            elif close_pos > after_marker:
                # No colon — [builtin/tool_name] with no args
                prefix = text[after_marker:close_pos].strip()
                if prefix:
                    results.append({
                        "server_name": "builtin",
                        "name": prefix,
                        "arguments": {},
                    })
                    used_ranges.append((marker_pos, close_pos + 1))
                    idx = close_pos + 1
                    continue
            idx = after_marker
            continue

        # Standard [MCP:...] format
        after_marker = marker_pos + len(_MCP_MARKER)

        colon_pos = text.find(":", after_marker)
        if colon_pos < 0:
            # No second colon — maybe the model wrote [MCP:server/tool] without :{}
            # (tools with no required args). Look for the closing bracket.
            close_pos = text.find("]", after_marker)
            if close_pos > after_marker:
                prefix = text[after_marker:close_pos].strip()
                if "/" in prefix:
                    parts = prefix.split("/", 1)
                    server_name = parts[0].strip()
                    name = parts[1].strip()
                    if server_name and name:
                        results.append({
                            "server_name": server_name,
                            "name": name,
                            "arguments": {},
                        })
                        used_ranges.append((marker_pos, close_pos + 1))
                        idx = close_pos + 1
                        continue
            idx = after_marker
            continue

        prefix = text[after_marker:colon_pos]

        balanced = _find_balanced_json(text, colon_pos + 1)
        if balanced is None:
            idx = colon_pos + 1
            continue

        json_str, json_start, json_end = balanced

        if "/" in prefix:
            parts = prefix.split("/", 1)
            server_name = parts[0].strip()
            name = parts[1].strip()
        else:
            server_name = "default"
            name = prefix.strip()

        try:
            args = json.loads(json_str)
        except json.JSONDecodeError:
            args = {}

        if isinstance(args, dict):
            results.append({
                "server_name": server_name,
                "name": name,
                "arguments": args,
            })
            used_ranges.append((marker_pos, json_end))

        idx = json_end

    for match in MCP_CALL_SIMPLE_PATTERN.finditer(text):
        m_start, m_end = match.start(), match.end()
        if any(r[0] <= m_start and m_end <= r[1] for r in used_ranges):
            continue
        try:
            args = json.loads(match.group(2))
        except json.JSONDecodeError:
            args = {}
        results.append({
            "server_name": "default",
            "name": match.group(1),
            "arguments": args,
        })

    return results


# ── Event type helpers ──────────────────────────────────────────────────


def delta_event(content: str) -> dict:
    return {"type": "delta", "content": content}


def tool_call_event(tc: ToolCall) -> dict:
    return {
        "type": "tool_call",
        "id": tc.id,
        "server_name": tc.server_name,
        "name": tc.name,
        "arguments": tc.arguments,
    }


def error_event(message: str) -> dict:
    return {"type": "error", "message": message}


def end_event() -> dict:
    return {"type": "end"}


# ── Name sanitization + schema cleaning (Hermes / opencode pattern) ──

def _sanitize_name(s: str) -> str:
    """Sanitize MCP name component for tool prefix generation.
    Only allows [A-Za-z0-9_], replaces others with '_'.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", str(s or ""))


def _clean_schema(schema: dict) -> dict:
    """Clean MCP input schema for LLM tool-calling compatibility.
    
    Fixes from Hermes's _normalize_mcp_input_schema:
      - Ensure type="object" for root schema
      - Remove $schema references (some APIs reject them)
      - Prune additionalProperties if true (avoid schema bloat)
      - Add properties={} if object is missing them
    """
    if not schema:
        return {"type": "object", "properties": {}}
    
    out = dict(schema)
    out.pop("$schema", None)
    
    if not out.get("type"):
        out["type"] = "object"
    
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    
    if out.get("additionalProperties") is True:
        del out["additionalProperties"]
    
    return out


# ── Abstract base provider ──────────────────────────────────────────────


class BaseProvider(ABC):
    """Abstract interface for a model provider.

    Each provider wraps one model backend and exposes a standard streaming
    chat interface. Providers that support function calling (OpenAI,
    Anthropic) handle tool call detection natively. Providers that don't
    (local MiniCPM) pass through raw text; the caller is responsible for
    detecting embedded MCP tool calls.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider name, e.g. 'local', 'openai', 'anthropic'."""

    @property
    def supports_function_calling(self) -> bool:
        """Whether this provider natively supports function/tool calling."""
        return False

    @property
    def supports_streaming(self) -> bool:
        """Whether this provider supports streaming responses."""
        return True

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        *,
        system: Optional[str] = None,
        tools: Optional[list[ToolDef]] = None,
        max_tokens: int = 512,
        temperature: float = 0.6,
        top_p: float = 0.95,
        **kwargs,
    ) -> AsyncGenerator[dict, None]:
        """Stream chat completions from the model.

        Yields event dicts:
          {"type": "delta", "content": "..."}
          {"type": "tool_call", "id": "...", "server_name": "...",
           "name": "...", "arguments": {...}}
          {"type": "error", "message": "..."}
          {"type": "end"}

        For providers with function calling, a "tool_call" event means the
        model wants to invoke a tool. The caller should execute the tool
        and feed the result back in a subsequent call.

        For providers without function calling, only "delta" events are
        yielded. The caller must scan the accumulated text for embedded
        MCP tool call markers.
        """
        yield end_event()

    async def count_tokens(self, messages: list[dict]) -> int:
        """Rough token count estimate (chars // 4). Override for accuracy."""
        total = 0
        for m in messages:
            total += len(m.get("content", "")) // 4
        return total

    @staticmethod
    def _openai_tool_format(tools: list[ToolDef]) -> list[dict]:
        """Convert MCP tool defs to OpenAI function-calling format.
        Tools are named: mcp_{server_name}_{tool_name} (opencode/Hermes pattern).
        """
        result = []
        seen_names = set()
        for t in tools:
            prefixed = f"mcp_{_sanitize_name(t.server_name)}_{_sanitize_name(t.name)}"
            if prefixed in seen_names:
                continue
            seen_names.add(prefixed)
            result.append({
                "type": "function",
                "function": {
                    "name": prefixed,
                    "description": t.description or f"[{t.server_name}] {t.name}",
                    "parameters": _clean_schema(t.input_schema),
                },
            })
        return result

    @staticmethod
    def _anthropic_tool_format(tools: list[ToolDef]) -> list[dict]:
        """Convert MCP tool defs to Anthropic tool format.
        Uses the same prefixed names as _openai_tool_format
        (mcp_{server_name}_{tool_name}) so _tool_map reverse lookup works.
        """
        result = []
        seen_names = set()
        for t in tools:
            prefixed = f"mcp_{_sanitize_name(t.server_name)}_{_sanitize_name(t.name)}"
            if prefixed in seen_names:
                continue
            seen_names.add(prefixed)
            result.append({
                "name": prefixed,
                "description": t.description or f"[{t.server_name}] {t.name}",
                "input_schema": _clean_schema(t.input_schema),
            })
        return result
