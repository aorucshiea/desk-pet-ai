"""Local model provider: wraps the existing LlamaServer for MiniCPM inference.

This provider does not natively support function calling. Instead, the system
prompt teaches the model to emit `[MCP:server/tool:{"arg":"value"}]` markers
when it wants to invoke a tool. The caller (server.py) detects these markers
in the accumulated output, executes the tool via MCPManager, and optionally
regenerates the final response with a function-calling-capable provider.
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Optional

from ..llama_client import LlamaServer
from ..log_setup import get_logger
from .base import BaseProvider, ToolDef, delta_event, end_event, error_event


TOOLS_INSTRUCTION = (
    "You have access to the following tools. To invoke a tool, output a line "
    "containing exactly:\n"
    '[MCP:server_name/tool_name:{"arg":"value"}]\n'
    "The arguments must be valid JSON. After invoking a tool, the system will "
    "return the result and you may continue responding.\n\n"
)


def _format_tools_block(tools: list[ToolDef]) -> str:
    lines = ["<available_tools>"]
    for t in tools:
        args_desc = json.dumps(t.input_schema, indent=2) if t.input_schema else "{}"
        lines.append(
            f"[MCP:{t.server_name}/{t.name}] "
            f"{t.description or f'Invoke {t.name}'}\n"
            f"    Arguments: {args_desc}"
        )
    lines.append("</available_tools>")
    return "\n".join(lines)


class LocalProvider(BaseProvider):
    """Provider backed by the local llama-server (MiniCPM)."""

    def __init__(
        self,
        server: LlamaServer,
        *,
        enable_thinking: bool = False,
        lora: Optional[list[dict]] = None,
    ) -> None:
        self._server = server
        self._enable_thinking = enable_thinking
        self._lora = lora
        self._log = get_logger()

    @property
    def name(self) -> str:
        return "local"

    @property
    def is_minicpm_model(self) -> bool:
        """True when the active local checkpoint is a MiniCPM-family model.

        The `[MCP:server/tool:{...}]` mini-language, the `[EMOTION:xxx]`
        trailer and the `[NEXT_CHAT:N]` proactive tag are MiniCPM-specific
        prompt engineering taught to the bundled MiniCPM persona in the
        Electron renderer's system prompt. Injecting them into a generic
        local GGUF (e.g. llama-3-vision, qwen-vl) makes the small model
        obediently emit those markers as if they were real tool calls,
        which the gateway then mis-parses. Gate every MiniCPM-only
        behavior on this check so non-MiniCPM local models run plain-text.
        """
        name = getattr(getattr(self._server, "model_path", None), "name", "") or ""
        return name.lower().startswith("minicpm")

    @property
    def supports_function_calling(self) -> bool:
        return False

    async def chat(
        self,
        messages: list[dict],
        *,
        system: Optional[str] = None,
        tools: Optional[list[ToolDef]] = None,
        max_tokens: int = 512,
        temperature: float = 0.6,
        top_p: float = 0.95,
        top_k: int = 0,
        repetition_penalty: float = 1.05,
        **kwargs,
    ) -> AsyncGenerator[dict, None]:
        if not self._server.alive:
            yield error_event("llama-server not running")
            return

        if tools:
            # Only inject if the renderer hasn't already done so (avoid double injection).
            if system and ("<available_tools>" in system or "[MCP:" in system):
                pass  # renderer already injected — skip
            else:
                tools_text = _format_tools_block(tools) + TOOLS_INSTRUCTION
                if system:
                    system = tools_text + system
                else:
                    system = tools_text

        if system:
            messages = [{"role": "system", "content": system}] + messages

        try:
            async for kind, piece in self._server.stream_chat(
                messages=messages,
                max_tokens=max(1, int(max_tokens)),
                temperature=max(0.0, float(temperature)),
                top_p=float(top_p),
                top_k=int(top_k),
                repetition_penalty=float(repetition_penalty),
                enable_thinking=self._enable_thinking,
                lora=self._lora,
            ):
                if kind == "content":
                    yield delta_event(piece)
                elif kind == "reasoning":
                    yield {"type": "think", "content": piece}
        except Exception as exc:
            self._log.exception("LocalProvider chat error: %s", exc)
            yield error_event(str(exc))
            return

        yield end_event()
