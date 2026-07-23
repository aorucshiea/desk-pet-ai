"""FastAPI gateway in front of llama.cpp's llama-server.

Exposes the same HTTP/SSE contract the Electron app already speaks with
the legacy PyTorch sidecar, so the renderer (clawd-on-desk/src/minicpm-chat.*)
does not need to change. The actual inference happens in the subprocess
owned by `LlamaServer`; this file is just glue.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, List, Optional, Union

import httpx

from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .clawd_state import ClawdBridge
from .llama_client import LlamaServer, detect_backend, find_sibling_mmproj
from .log_setup import get_logger
from .mcp.mcp_manager import MCPManager
from .mcp.mcp_router import create_mcp_router
from .providers import ProviderRegistry, create_providers
from .providers.anthropic import AnthropicProvider
from .providers.base import ToolDef, parse_mcp_calls
from .providers.local import LocalProvider
from .skills.skill_routes import register_skill_routes
from .omniparser_manager import OmniParserManager
from .memory import MemoryStore
from .memory.tool import (
    MEMORY_TOOL_SCHEMA,
    memory_tool_handler,
    set_memory_store,
)
from . import screen_click
from .screen_capture import capture as screen_capture
from .think_filter import ThinkBlockFilter
from .updater import DEFAULT_SOURCE as DEFAULT_UPDATE_SOURCE
from .updater import ModelUpdater


# ── Request / response shapes ────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str = Field(..., description="'system' | 'user' | 'assistant'")
    # OpenAI vision-compatible: content may be a plain text string OR a
    # list of content blocks, e.g.
    #   [{"type":"text","text":"..."},
    #    {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]
    # We forward the raw value into the gateway so multimodal models
    # (via --mmproj) actually receive the image payload. Plain-text
    # callers are unaffected: pydantic accepts the str form verbatim.
    content: Union[str, List[dict]]


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    max_new_tokens: int = 4096
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int = 0
    repetition_penalty: float = 1.05
    stream: bool = True
    system: Optional[str] = None
    thinking: bool = False
    silent: bool = False  # bypass pet state pushes (used by narrator)
    # When true the gateway sends `lora: []` to llama-server for THIS
    # request only, which disables every pre-loaded LoRA adapter for the
    # current generation without touching global scales. Used by the
    # narrator so its informational replies don't pick up the active
    # persona's stylistic bias. No-op when no adapter is currently
    # active.
    disable_adapter: bool = False
    # Model provider routing (Phase 2)
    model_provider: Optional[str] = Field(
        default=None,
        description="'local' | 'openai' | 'anthropic' | 'auto' | None (=local fallback)",
    )
    auto_route: bool = Field(
        default=False,
        description="When true, use a smart model to auto-route to the best provider",
    )
    tools_enabled: bool = Field(
        default=True,
        description="When false, skip MCP tool injection and detection",
    )
    context_window: Optional[int] = None


# When thinking=true the model emits a <think> block before the
# answer; both share one max_new_tokens budget. Bump the floor so reasoning
# doesn't eat the entire allowance and truncate the reply.
THINKING_MIN_MAX_NEW_TOKENS = 1280
MAX_NEW_TOKENS_CAP = 8192


def _effective_max_new_tokens(req: ChatRequest) -> int:
    base = int(max(1, min(req.max_new_tokens, MAX_NEW_TOKENS_CAP)))
    if req.thinking:
        return min(MAX_NEW_TOKENS_CAP, max(base, THINKING_MIN_MAX_NEW_TOKENS))
    return base


# ── Model discovery ─────────────────────────────────────────────────────────


def discover_models(roots: List[Path]) -> List[dict]:
    """Return [{name, path}] for every *.gguf file under `roots`."""
    seen: set[Path] = set()
    out: List[dict] = []
    for root in roots:
        try:
            r = root.expanduser().resolve()
        except Exception:
            continue
        if not r.exists() or r in seen:
            continue
        seen.add(r)
        if r.is_file() and r.suffix.lower() == ".gguf":
            out.append({"name": r.name, "path": str(r)})
            continue
        if not r.is_dir():
            continue
        for p in sorted(r.rglob("*.gguf")):
            if any(part.endswith(".update-staging") or part.endswith(".bak") for part in p.parts):
                continue
            out.append({"name": p.name, "path": str(p)})
    return out


def _default_model_roots() -> List[Path]:
    """Locations to scan for *.gguf when no explicit MINICPM_MODEL_DIR
    is set. The Electron host passes `--model` explicitly so this is
    only used by direct CLI / dev runs."""
    here = Path(__file__).resolve().parent.parent
    return [
        Path.home() / "Library" / "Application Support" / "Clawd on Desk" / "models",
        Path.home() / ".local" / "share" / "Clawd on Desk" / "models",
        here / "models",
        here.parent / "models",
    ]


# ── LoRA adapter discovery ──────────────────────────────────────────────────


# Filename-keyword → persona slug. The slug is the stable identifier the
# Electron renderer keys off ("default" / "neko" / "muice" / ...) when
# deciding things like whether to flip `thinking` off (persona LoRAs don't
# carry <think> training, so reasoning collides with their style).
# Matching is substring + case-insensitive against the filename stem.
PERSONA_HINTS: dict[str, str] = {
    "nekoqa": "neko",
    "neko": "neko",
    "muice": "muice",
    "chuuni": "chuuni",
    "moyu": "moyu",
    "zhiyuan": "zhiyuan",
}


def _persona_for(path: Path) -> str:
    stem = path.stem.lower()
    parent = path.parent.name.lower()
    haystack = f"{parent}/{stem}"
    for needle, slug in PERSONA_HINTS.items():
        if needle in haystack:
            return slug
    return "custom"


def _default_adapter_roots() -> List[Path]:
    """Where to scan for `*.gguf` LoRA adapters when no `MINICPM_ADAPTER_DIR`
    env is set. The Electron host normally injects that env, so this only
    runs for direct CLI / dev / test invocations.

    The order here mirrors `_default_model_roots`: per-user app data first,
    then the dev-only repo path next to the sidecar package.
    """
    here = Path(__file__).resolve().parent.parent
    return [
        Path.home() / "Library" / "Application Support" / "Clawd on Desk" / "adapters",
        Path.home() / ".local" / "share" / "Clawd on Desk" / "adapters",
        here.parent / "adapters",   # <repo>/adapters/ in dev checkouts
    ]


def discover_adapters(roots: List[Path]) -> List[dict]:
    """Return [{name, path, persona}] for every `*.gguf` LoRA under `roots`.

    Skips electron-builder staging / backup directories the same way
    `discover_models` does, so we don't accidentally surface half-downloaded
    adapters."""
    seen_files: set[Path] = set()
    out: List[dict] = []
    for root in roots:
        try:
            r = root.expanduser().resolve()
        except Exception:
            continue
        if not r.exists() or not r.is_dir():
            continue
        for p in sorted(r.rglob("*.gguf")):
            if any(part.endswith(".update-staging") or part.endswith(".bak") for part in p.parts):
                continue
            try:
                resolved = p.resolve()
            except Exception:
                continue
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            out.append({
                "name": p.name,
                "path": str(p),
                "persona": _persona_for(p),
            })
    return out


def _resolve_adapter_root(initial_model: Optional[Path]) -> Optional[Path]:
    """Pick the canonical writable adapter dir for `/api/load-adapter`
    "open in Finder" hints. Resolution order:

    1. `MINICPM_ADAPTER_DIR` env (Electron host injects this in packaged
       mode pointing at `<userData>/adapters/`)
    2. First default root that already exists
    3. First default root regardless of existence (the caller can then
       `mkdir -p` before opening Finder)
    """
    env_dir = os.environ.get("MINICPM_ADAPTER_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    for cand in _default_adapter_roots():
        if cand.exists() and cand.is_dir():
            return cand
    defaults = _default_adapter_roots()
    return defaults[-1] if defaults else None


# Mirror file Electron writes after every manifest mutation. Lives in
# the adapter dir under a dot prefix so `discover_adapters`'s `*.gguf`
# scan misses it. Schema mirrors `<userData>/minicpm-adapters.json` 1:1
# (see clawd-on-desk/src/minicpm-chat.js).
_MANIFEST_MIRROR = ".manifest.json"


def read_adapter_manifest(adapter_root: Optional[Path]) -> dict:
    """Return the parsed `.manifest.json` from `adapter_root`, or an
    empty manifest if the file is absent / malformed.

    The gateway is a pure reader here — Electron owns the data and
    re-writes the mirror on every CRUD operation. Reading on every
    `/api/adapters` request keeps us a snapshot fresh without an
    explicit refresh endpoint."""
    if adapter_root is None:
        return {"version": 1, "items": []}
    try:
        mirror = Path(adapter_root) / _MANIFEST_MIRROR
        if not mirror.is_file():
            return {"version": 1, "items": []}
        with mirror.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"version": 1, "items": []}
    if not isinstance(data, dict):
        return {"version": 1, "items": []}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return {"version": int(data.get("version") or 1), "items": items}


def _manifest_by_resolved_path(manifest: dict) -> dict[Path, dict]:
    """Index manifest items by their resolved absolute path so the
    `/api/adapters` merge step is O(1) per scanned file."""
    out: dict[Path, dict] = {}
    for entry in manifest.get("items", []) or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path")
        if not isinstance(raw, str) or not raw:
            continue
        try:
            out[Path(raw).expanduser().resolve()] = entry
        except Exception:
            continue
    return out


# ── App factory ──────────────────────────────────────────────────────────────


def build_app(
    *,
    initial_model: Optional[Path],
    update_source: str = DEFAULT_UPDATE_SOURCE,
    ctx_size: int = 131072,
    n_gpu_layers: int = -1,
    threads: Optional[int] = None,
) -> FastAPI:
    log = get_logger()
    bridge = ClawdBridge(enabled=True, debug=False)

    # Resolve the adapter root so /api/adapters can scan it; we still
    # show the full list to the UI even when none are loaded yet, so
    # users can browse + activate any LoRA from Settings.
    adapter_root = _resolve_adapter_root(initial_model)

    # Boot-time LoRA load is now *opt-in*: only the LoRA the Electron
    # host has persisted as the active one (env MINICPM_ACTIVE_ADAPTER)
    # gets passed to llama-server via --lora. Default behaviour is pure
    # Base — no third-party LoRA is preloaded just because it happens
    # to live on disk. Switching to a different LoRA later triggers
    # `LlamaServer.reload_adapters([new])`, costing one llama-server
    # restart but keeping the steady-state memory minimal.
    _env_active = os.environ.get("MINICPM_ACTIVE_ADAPTER", "").strip()
    initial_active: Optional[Path] = None
    if _env_active:
        try:
            cand = Path(_env_active).expanduser().resolve(strict=True)
            if cand.suffix.lower() == ".gguf":
                initial_active = cand
            else:
                log.warning("MINICPM_ACTIVE_ADAPTER ignored (not .gguf): %s", cand)
        except FileNotFoundError:
            log.warning("MINICPM_ACTIVE_ADAPTER points at missing file: %s", _env_active)

    server = LlamaServer(
        model_path=initial_model,
        ctx_size=ctx_size,
        n_gpu_layers=n_gpu_layers,
        threads=threads,
        adapters=[initial_active] if initial_active else [],
        # Boot-time multimodal pairing: picked up if the bundled model
        # ships with a corresponding `mmproj-*.gguf` in the same dir.
        mmproj_path=find_sibling_mmproj(initial_model) if initial_model else None,
    )

    # In-memory adapter state. Single source of truth for what the
    # Electron app sees as "the active LoRA". Boots from the persisted
    # choice; cleared on /api/load-adapter {path:null}; updated to a
    # new path on /api/load-adapter {path:<gguf>}. The Electron host
    # is responsible for writing the latest choice back to its prefs
    # file so the next sidecar spawn boots into the same state.
    state: dict[str, Optional[Path]] = {"current_adapter": initial_active}
    startup_error: Optional[str] = None

    # ── Screen observation state ─────────────────────────────────────
    consent_state: str = "deny"
    consent_lock = asyncio.Lock()
    omniparser_manager = OmniParserManager()

    # ── Long-term memory store ───────────────────────────────────────
    # One store per sidecar process. The frozen snapshot (MEMORY.md +
    # USER.md) is captured here at boot and served to the system prompt
    # via /api/memory. Mid-session tool writes persist to disk immediately
    # but do NOT mutate the snapshot — see memory/store.py for the rationale
    # (prefix-cache preservation across turns).
    memory_dir_env = os.environ.get("MINICPM_MEMORY_DIR", "").strip()
    if memory_dir_env:
        memory_dir = Path(memory_dir_env).expanduser()
    else:
        # Dev fallback when the Electron host didn't inject the env (direct
        # CLI run). Mirrors the per-platform app-data layout used elsewhere.
        if platform.system() == "Darwin":
            memory_dir = Path.home() / "Library" / "Application Support" / "MiniCPM Desk Pet" / "memories"
        elif platform.system() == "Windows":
            memory_dir = Path.home() / "AppData" / "Roaming" / "MiniCPM Desk Pet" / "memories"
        else:
            memory_dir = Path.home() / ".local" / "share" / "MiniCPM Desk Pet" / "memories"
    memory_store = MemoryStore()
    try:
        memory_store.load_from_disk(memory_dir)
        log.info("memory store loaded from %s", memory_dir)
    except Exception as exc:
        log.warning("memory store load failed (continuing with empty memory): %s", exc)
    set_memory_store(memory_store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal startup_error
        # Don't fail boot when the model isn't on disk yet — onboarding
        # downloads it via /api/update-apply and only then calls
        # /api/load-model. The pet still wants /api/health to answer 200
        # in the meantime so the bubble doesn't show a permanent error.
        if initial_model and Path(initial_model).exists():
            try:
                await server.start()
                startup_error = None
            except Exception as exc:
                startup_error = str(exc)
                log.exception("initial llama-server start failed: %s", exc)
        else:
            log.info("model not present at startup; waiting for /api/load-model")
        bridge.post("idle", title="MiniCPM 桌宠")
        # Load MCP config on startup
        try:
            await mcp_manager.load_config()
            if mcp_manager._clients:
                results = await mcp_manager.connect_all()
                connected = sum(1 for v in results.values() if v)
                log.info("MCP: %d/%d servers connected", connected, len(results))
        except Exception as exc:
            log.warning("MCP startup failed: %s", exc)
        # Curator: idle-triggered skill lifecycle maintenance. Don't block
        # boot — schedule a background pass that checks should_run_now() and
        # runs transitions if the interval has elapsed. First boot seeds
        # state and defers one interval (won't archive freshly-added skills).
        async def _curator_startup_check():
            try:
                from .evolve import should_run_now, run_curator
                if should_run_now():
                    live_paths = {n: s.path for n, s in skill_service._skills.items()}
                    summary = run_curator(skill_service.usage, live_paths)
                    log.info("curator startup pass: %s", summary.get("counts"))
            except Exception as exc:
                log.warning("curator startup check failed: %s", exc)
        import asyncio as _aio
        _aio.get_event_loop().create_task(_curator_startup_check())
        try:
            yield
        finally:
            bridge.post("sleeping")
            # Tear down MCP subprocesses gracefully *before* everything else.
            # The mcp SDK's transport context managers back onto anyio
            # TaskGroups whose cancel scopes must be exited in the same task
            # that entered them; if we skip this, the runners get GC-finalized
            # at interpreter shutdown and anyio raises
            # "Attempted to exit cancel scope in a different task" — which
            # crashes the sidecar. disconnect_all() drives each runner's
            # clean exit in its own task.
            try:
                await mcp_manager.disconnect_all()
            except Exception as exc:
                log.warning("MCP shutdown error: %s", exc)
            try:
                await server.stop()
            finally:
                await omniparser_manager.close()
                bridge.close()

    app = FastAPI(title="MiniCPM Sidecar Gateway", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register skill routes so /api/skills endpoints are available
    # immediately (even before a model is loaded).
    skill_service = register_skill_routes(app)

    # ─── Provider registry + MCP (Phase 2) ─────────────────────────
    provider_registry = ProviderRegistry()
    # Local provider is always available
    provider_registry.register(LocalProvider(server, enable_thinking=False))

    # Load API providers from ~/.minicpm/providers.json
    _providers_path = Path.home() / ".minicpm" / "providers.json"

    def _register_providers():
        try:
            if not _providers_path.is_file():
                return
            providers_data = json.loads(_providers_path.read_text(encoding="utf-8"))
            providers_map = providers_data.get("providers", providers_data) if isinstance(providers_data, dict) else {}
            for provider_id, cfg in providers_map.items():
                if not isinstance(cfg, dict):
                    continue
                api_key = cfg.get("apiKey", "")
                if not api_key or provider_id == "local":
                    continue
                base_url = cfg.get("baseUrl", "https://api.openai.com/v1")
                model = cfg.get("model", "gpt-4o")
                thinking = cfg.get("thinking")
                reasoning_effort = cfg.get("reasoningEffort") or cfg.get("reasoning_effort")
                context_window = cfg.get("contextWindow")
                if "anthropic.com" in str(base_url).lower():
                    from .providers.anthropic import AnthropicProvider
                    p = AnthropicProvider(
                        api_key=api_key, base_url=base_url, model=model, name=provider_id,
                    )
                else:
                    from .providers.openai import OpenAIProvider
                    p = OpenAIProvider(
                        api_key=api_key, base_url=base_url, model=model, name=provider_id,
                        thinking=thinking if thinking is not None else None,
                        reasoning_effort=reasoning_effort,
                        context_window=int(context_window) if context_window else None,
                    )
                provider_registry.register(p)
                log.info("Provider loaded: %s (model=%s)", provider_id, model)
        except Exception as exc:
            log.warning("Failed to load providers.json: %s", exc)
    _register_providers()

    # ── Builtin tool: observe_screen ──────────────────────────────────
    async def _handle_observe_screen(args):
        """MCP tool: capture user screen + OmniParser OCR, return labels."""
        nonlocal consent_state, consent_lock
        # Consent check — but don't consume "once" for tool calls.
        # The AI calling this tool is itself evidence of intent; the
        # "once" consent was meant for the HTTP auto-injection path.
        async with consent_lock:
            if consent_state == "deny":
                return {
                    "content": [{"type": "text", "text": (
                        "⚠️ 屏幕观察未授权。请告诉用户去设置中开启「允许查看屏幕」权限。"
                    )}],
                    "is_error": True,
                    "summary": "Screen observation denied — consent required.",
                }
            # Promote "once" to "always" so tool calls don't exhaust consent
            if consent_state == "once":
                consent_state = "always"
        # Capture screen
        b64 = await asyncio.to_thread(screen_capture)
        # Parse via OmniParser
        await omniparser_manager.ensure_alive()
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post("http://127.0.0.1:8000/parse/", json={
                    "base64_image": b64,
                    "ocr_mode": "local",
                    "chinese_ocr": True,
                })
                data = resp.json()
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"屏幕解析失败: {exc}"}],
                "is_error": True,
                "summary": f"OmniParser parse failed: {exc}",
            }
        parsed_items = data.get("parsed_content_list", [])
        # Build text representation
        lines = ["【屏幕内容 — OmniParser 解析】"]
        if not parsed_items:
            lines.append("  (屏幕上未检测到文字或图标 — 可能是空白桌面或锁屏)")
        else:
            for i, item in enumerate(parsed_items):
                t = item.get("type", "?")
                c = (item.get("content") or "")[:100]
                b = item.get("bbox", [])
                bbox_str = (f"[{b[0]:.3f},{b[1]:.3f},{b[2]:.3f},{b[3]:.3f}]"
                            if len(b) >= 4 else "[]")
                lines.append(f"  [{i}] {t}: \"{c}\" @ {bbox_str}")
        text_result = "\n".join(lines)
        return {
            "content": [{"type": "text", "text": text_result}],
            "summary": text_result,
            "screenshot_base64": b64,
            "parsed_content_list": parsed_items,
            "is_error": False,
        }

    # ── Builtin tool: capture_screen (raw vision, no OCR) ──────────────
    async def _handle_capture_screen(args):
        """MCP tool: capture a raw screenshot and return it as an image
        for the model to see directly. If the local model has no vision
        capability (no mmproj), tells the model to use observe_screen
        (OCR) instead."""
        nonlocal consent_state, consent_lock

        # Check if the local model can actually see images
        has_vision = bool(getattr(server, "mmproj_path", None))
        if not has_vision:
            return {
                "content": [{"type": "text", "text": (
                    "当前模型没有视觉能力（未加载 mmproj），无法直接看图片。"
                    "请改用 [MCP:builtin/observe_screen:{}] 获取屏幕文字内容。"
                )}],
                "summary": "No vision capability — use observe_screen instead.",
                "is_error": False,
            }

        async with consent_lock:
            if consent_state == "deny":
                return {
                    "content": [{"type": "text", "text": (
                        "⚠️ 屏幕截图未授权。请告诉用户去设置中开启「允许查看屏幕」权限。"
                    )}],
                    "is_error": True,
                    "summary": "Screen capture denied — consent required.",
                }
            if consent_state == "once":
                consent_state = "always"
        b64 = await asyncio.to_thread(screen_capture)

        # Downscale + recompress to JPEG so the vision encoder processes
        # far fewer tokens. A 1920x1080 PNG (~2MB) → 768px JPEG (~50KB)
        # cuts vision-token count by ~10x with negligible quality loss
        # for "what's on screen" understanding.
        try:
            import io as _io
            from PIL import Image as _Image
            raw = base64.b64decode(b64)
            img = _Image.open(_io.BytesIO(raw))
            max_side = 768
            if max(img.size) > max_side:
                ratio = max_side / max(img.size)
                img = img.resize((int(img.size[0] * ratio), int(img.size[1] * ratio)), _Image.LANCZOS)
            img = img.convert("RGB")
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:
            log.warning("capture_screen image resize failed, using raw: %s", exc)

        return {
            "content": [{"type": "text", "text": "截图完成，请直接查看下方的屏幕图片。"}],
            "summary": "Screenshot captured — see the image below.",
            "screenshot_base64": b64,
            "screenshot_mime": "image/jpeg",
            "is_error": False,
        }

    mcp_manager = MCPManager()
    mcp_manager.register_builtin({
        "name": "screen_click",
        "description": (
            "Perform a mouse action on a screen element. "
            "Supports left_click (default), right_click, double_click, scroll. "
            "The element must have a bbox [x1, y1, x2, y2] with normalized coordinates "
            "(0.0-1.0) from observe_screen. "
            "For scroll, set action='scroll' with optional direction ('up'/'down') and amount. "
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element": {
                    "type": "object",
                    "description": "An OmniParser element object with bbox",
                    "properties": {
                        "type": {"type": "string", "description": "text or icon"},
                        "bbox": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                            "description": "normalized bbox [x1, y1, x2, y2]",
                        },
                        "content": {"type": "string", "description": "the element's text or label"},
                        "action": {
                            "type": "string",
                            "enum": ["left_click", "right_click", "double_click", "scroll"],
                            "description": "Mouse action to perform (default: left_click)",
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["up", "down"],
                            "description": "Scroll direction (only for action=scroll, default: down)",
                        },
                        "amount": {
                            "type": "integer",
                            "description": "Scroll amount in notches (only for action=scroll, default: 3)",
                        },
                    },
                    "required": ["bbox"],
                }
            },
            "required": ["element"],
        },
        "handler": lambda args: screen_click.click_element(args.get("element", {})),
    })
    mcp_manager.register_builtin({
        "name": "observe_screen",
        "description": (
            "Capture and analyze the user's screen using OmniParser. "
            "Returns structured labels of all visible text and icons with their "
            "normalized bbox coordinates [x1,y1,x2,y2]. "
            "Call this when the user asks you to look at their screen, check what "
            "they are doing, or before clicking any screen element. "
            "If consent is denied the tool returns an error — guide the user to "
            "enable screen permission in settings."
        ),
        "input_schema": {"type": "object", "properties": {}},
        "handler": _handle_observe_screen,
    })
    mcp_manager.register_builtin({
        "name": "capture_screen",
        "description": (
            "Capture a raw screenshot of the user's screen and return it as an "
            "image for you to see directly with your vision capability. "
            "Unlike observe_screen (which runs OCR and returns text labels), "
            "this tool gives you the actual image so you can SEE the screen. "
            "Call this when you want to visually inspect what's on screen — "
            "reading UI layouts, understanding visual context, seeing images, etc. "
            "Requires screen consent; if denied, tell the user to enable it in settings."
        ),
        "input_schema": {"type": "object", "properties": {}},
        "handler": _handle_capture_screen,
    })
    mcp_router = create_mcp_router(mcp_manager)
    app.include_router(mcp_router)

    # ── Builtin tool: memory (cross-session long-term memory) ──────────
    # Lets the model write durable facts to MEMORY.md / USER.md. The frozen
    # snapshot is injected into the system prompt on every chat turn via
    # /api/memory (read by the renderer's fetchSkillsContext). This is the
    # mechanism that makes the pet "remember" across restarts.
    mcp_manager.register_builtin({
        "name": MEMORY_TOOL_SCHEMA["name"],
        "description": MEMORY_TOOL_SCHEMA["description"],
        "input_schema": MEMORY_TOOL_SCHEMA["input_schema"],
        "handler": memory_tool_handler,
    })

    # ── Builtin tool: skill_create (external distillation write surface) ─
    # Lets the model persist a workflow it just executed (or one the user
    # described via /learn) as an auditable SKILL.md. This is the 创建 step
    # of the 引导→创建→索引→加载→Patch closed loop — the capability is a
    # text file, not a weight, so it's readable, editable, and migratable.
    async def _handle_skill_create(args: dict) -> dict:
        result = skill_service.create_skill(
            str(args.get("name") or "").strip(),
            str(args.get("description") or "").strip(),
            str(args.get("body") or ""),
            tags=args.get("tags") if isinstance(args.get("tags"), list) else None,
            version=str(args.get("version") or "0.1.0").strip() or "0.1.0",
            overwrite=bool(args.get("overwrite", False)),
        )
        is_error = not result.get("ok", False)
        summary = result.get("error") or f"skill saved: {result.get('name')}"
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "is_error": is_error,
            "summary": summary,
        }
    mcp_manager.register_builtin({
        "name": "skill_create",
        "description": (
            "Create a reusable skill (SKILL.md) from a workflow you just executed "
            "or one the user described. This is how you turn a one-off procedure "
            "into a durable, auditable capability — external distillation. "
            "Arguments: name (lowercase-hyphenated), description (one sentence, "
            "what it does), body (full Markdown with frontmatter-less sections: "
            "When to Use / Prerequisites / How to Run / Steps / Pitfalls / Verification). "
            "Optional: tags (list), version (default 0.1.0), overwrite (default false). "
            "Refuse to overwrite an existing skill unless overwrite=true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "lowercase-hyphenated, <=64 chars"},
                "description": {"type": "string", "description": "one sentence, <=80 chars, states the capability"},
                "body": {"type": "string", "description": "full Markdown body WITHOUT frontmatter (we add it)"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "version": {"type": "string", "default": "0.1.0"},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["name", "description", "body"],
        },
        "handler": _handle_skill_create,
    })

    @app.get("/api/memory")
    async def get_memory_snapshot():
        """Return the frozen MEMORY.md + USER.md blocks for system-prompt injection.

        The renderer calls this once per fetchSkillsContext cache window and
        prepends the blocks to the chat system prompt. Snapshot is frozen at
        boot — mid-session writes persist to disk but only refresh the
        snapshot on next sidecar restart, preserving the provider prefix cache.
        """
        return {
            "memory": memory_store.format_for_system_prompt("memory") or "",
            "user": memory_store.format_for_system_prompt("user") or "",
            "memory_dir": str(memory_dir),
        }

    @app.get("/api/providers")
    async def list_providers():
        return {
            "providers": provider_registry.list(),
            "default": "local",
        }

    @app.post("/api/providers/reload")
    async def reload_providers():
        """Reload API providers from ~/.minicpm/providers.json."""
        _register_providers()
        return {"ok": True, "providers": provider_registry.list()}

    # Model roots used by /api/models — honour the env override the
    # Electron host sets to <userData>/models/ in packaged mode.
    env_root = os.environ.get("MINICPM_MODEL_DIR")
    extra_roots: List[Path] = []
    if env_root:
        extra_roots.append(Path(env_root))
    if initial_model:
        extra_roots.append(Path(initial_model).expanduser().resolve().parent)
    extra_roots.extend(_default_model_roots())

    def _get_active_model_path() -> Path:
        if server.model_path:
            return server.model_path
        if initial_model:
            return Path(initial_model)
        # Fall back to the first discovered gguf so /api/update-check
        # always has *some* anchor to compare against.
        items = discover_models(extra_roots)
        if items:
            return Path(items[0]["path"])
        # Last resort: synthesise a stub path so updater code can still
        # compute target_dir for download staging.
        return (extra_roots[0] if extra_roots else Path.cwd()) / "minicpm.gguf"

    updater = ModelUpdater(_get_active_model_path(), source=update_source)

    # ─── Health / introspection ────────────────────────────────────────

    @app.get("/api/health")
    async def health():
        sub_health = await server.health()
        backend = detect_backend()
        adapter = state["current_adapter"]
        current = backend.get("current") or backend["recommended"]
        return {
            "ok": True,
            "alive": server.alive,
            "backend": "llama.cpp",
            "accel": current,
            "device": current,  # alias used by older Electron code paths
            "dtype": "gguf",
            "model_dir": str(server.model_path) if server.model_path else None,
            "model_name": server.model_path.name if server.model_path else None,
            "adapter": str(adapter) if adapter else None,
            "persona": _persona_for(adapter) if adapter else "default",
            "llama_server": sub_health,
            "port": server.port,
            "startup_error": startup_error,
        }

    @app.get("/api/devices")
    def list_devices():
        info = detect_backend()
        return info

    @app.post("/api/set-device")
    async def set_device(payload: dict):
        device = str(payload.get("device") or "").strip().lower()
        if device not in ("metal", "cuda", "cpu", "vulkan", "mps", "auto", ""):
            return JSONResponse({"error": f"unknown device: {device!r}"}, status_code=400)
        # "mps" is the legacy name for Apple Silicon; transparently map
        # to metal for consistency with llama.cpp terminology.
        if device == "mps":
            device = "metal"
        if device == "vulkan" and platform.system() != "Windows":
            return JSONResponse(
                {"error": "vulkan backend is only configurable on Windows"},
                status_code=400,
            )
        if device:
            os.environ["MINICPM_DEVICE"] = device
        else:
            os.environ.pop("MINICPM_DEVICE", None)
        return {"ok": True, "device": device or "auto", "note": "restart sidecar to take effect"}

    @app.get("/api/onboarding")
    def onboarding():
        path = server.model_path or _get_active_model_path()
        present = path.exists() if path else False
        adapter = state["current_adapter"]
        backend = detect_backend()
        return {
            "model_present": present,
            "model_dir": str(path) if path else None,
            "device": backend.get("current") or backend["recommended"],
            "dtype": "gguf",
            "adapter": str(adapter) if adapter else None,
            "persona": _persona_for(adapter) if adapter else "default",
            "stage_hint": "ready" if present else "model-download",
        }

    # ─── Model / adapter listing ───────────────────────────────────────

    @app.get("/api/models")
    def list_models():
        items = discover_models(extra_roots)
        current = str(server.model_path) if server.model_path else None
        return {
            "items": items,
            "current": current,
            "current_name": server.model_path.name if server.model_path else None,
        }

    @app.post("/api/load-model")
    async def load_model(payload: dict):
        nonlocal startup_error
        path = str(payload.get("path") or "").strip()
        mmproj_path = payload.get("mmproj")
        mmproj_path = str(mmproj_path).strip() if mmproj_path else ""
        if not path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        target = Path(path).expanduser().resolve()
        if not target.is_file() or target.suffix.lower() != ".gguf":
            return JSONResponse({"error": f"not a .gguf file: {target}"}, status_code=400)
        # Resolve mmproj: take the explicit value when provided, otherwise
        # look up a sibling `mmproj-*.gguf`. Empty string clears it.
        if mmproj_path:
            mmproj_target = Path(mmproj_path).expanduser().resolve()
            if not mmproj_target.is_file() or mmproj_target.suffix.lower() != ".gguf":
                return JSONResponse({"error": f"not a .gguf file: {mmproj_target}"}, status_code=400)
        else:
            sibling = find_sibling_mmproj(target)
            mmproj_target = sibling  # may be None
        bridge.post("working", event="LoadModel", title=f"加载 {target.name}")
        try:
            await server.swap_model(target, mmproj_target)
            updater.local_model_path = target
            startup_error = None
        except Exception as exc:
            startup_error = str(exc)
            bridge.post("error")
            return JSONResponse({"error": str(exc)}, status_code=500)
        bridge.post("idle")
        return {
            "ok": True,
            "model_dir": str(target),
            "model_name": target.name,
            "mmproj_dir": str(mmproj_target) if mmproj_target else None,
            "mmproj_name": mmproj_target.name if mmproj_target else None,
        }

    @app.post("/api/upload-model")
    async def upload_model(file: UploadFile = File(...)):
        """Accept a .gguf file upload, save to models dir, and auto-load."""
        nonlocal startup_error
        if not file.filename or not file.filename.lower().endswith(".gguf"):
            return JSONResponse({"error": "only .gguf files are accepted"}, status_code=400)

        # Determine save directory: use the first available model root
        roots = _default_model_roots() + extra_roots
        save_dir = None
        for r in roots:
            try:
                rp = r.expanduser().resolve()
                if rp.exists() or rp.parent.exists():
                    rp.mkdir(parents=True, exist_ok=True)
                    save_dir = rp
                    break
            except Exception:
                continue
        if save_dir is None:
            save_dir = Path.cwd() / "models"
            save_dir.mkdir(parents=True, exist_ok=True)

        target = (save_dir / file.filename).resolve()
        # Write uploaded file
        try:
            with open(target, "wb") as f:
                while chunk := await file.read(1024 * 1024):  # 1 MiB chunks
                    f.write(chunk)
        except Exception as exc:
            return JSONResponse({"error": f"failed to save file: {exc}"}, status_code=500)

        # Auto-load the model
        mmproj_target = find_sibling_mmproj(target)
        bridge.post("working", event="LoadModel", title=f"加载 {target.name}")
        try:
            await server.swap_model(target, mmproj_target)
            updater.local_model_path = target
            startup_error = None
        except Exception as exc:
            startup_error = str(exc)
            bridge.post("error")
            return JSONResponse({"error": str(exc)}, status_code=500)
        bridge.post("idle")
        return {
            "ok": True,
            "model_dir": str(target),
            "model_name": target.name,
            "mmproj_dir": str(mmproj_target) if mmproj_target else None,
            "mmproj_name": mmproj_target.name if mmproj_target else None,
        }

    def _scan_adapters() -> List[dict]:
        # Re-resolve the root each call so Settings → "open adapter dir"
        # → drop new .gguf → "refresh" picks up files added at runtime
        # without restarting the sidecar. Also re-read the manifest
        # mirror on every call so rename / upload mutations show up in
        # the next /api/adapters response without any explicit refresh
        # ping from Electron.
        root = _resolve_adapter_root(server.model_path)
        if not root:
            return []
        items = discover_adapters([root])
        manifest = read_adapter_manifest(root)
        by_path = _manifest_by_resolved_path(manifest)
        for item in items:
            try:
                key = Path(item["path"]).expanduser().resolve()
            except Exception:
                continue
            entry = by_path.get(key)
            if not entry:
                continue
            # Only surface the product-layer fields; gateway's persona
            # slug already on `item` wins by default but a manifest
            # override (user typed their own) takes precedence.
            if isinstance(entry.get("displayName"), str) and entry["displayName"].strip():
                item["displayName"] = entry["displayName"].strip()
            if isinstance(entry.get("aliases"), list):
                item["aliases"] = [str(a).strip() for a in entry["aliases"] if str(a).strip()]
            if isinstance(entry.get("source"), str):
                item["source"] = entry["source"]
            if isinstance(entry.get("id"), str):
                item["id"] = entry["id"]
            if isinstance(entry.get("persona"), str) and entry["persona"].strip():
                item["persona"] = entry["persona"].strip()
        return items

    @app.get("/api/adapters")
    def list_adapters():
        items = _scan_adapters()
        current = state["current_adapter"]
        return {
            "items": items,
            "current": str(current) if current else None,
            "current_name": current.name if current else None,
            "adapter_dir": str(_resolve_adapter_root(server.model_path) or ""),
        }

    @app.post("/api/load-adapter")
    async def load_adapter(payload: dict):
        raw = payload.get("path")
        # path = null  →  deactivate any LoRA (back to base model)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            # If llama-server was booted with `--lora <something>`, a
            # per-request `lora: []` is enough to force base output on
            # modern llama.cpp. We still respawn with no `--lora` here
            # so switching back to Base releases the adapter weights too.
            if server.adapter_paths:
                bridge.post("working", event="UnloadAdapter", title="卸载 LoRA")
                try:
                    await server.reload_adapters([])
                except Exception as exc:
                    bridge.post("error")
                    return JSONResponse({"error": str(exc)}, status_code=500)
                bridge.post("idle")
            state["current_adapter"] = None
            return {"ok": True, "adapter": None, "persona": "default"}

        target = Path(str(raw)).expanduser()
        try:
            target = target.resolve(strict=True)
        except FileNotFoundError:
            return JSONResponse(
                {"error": f"adapter file not found: {target}"},
                status_code=400,
            )
        if target.suffix.lower() != ".gguf":
            return JSONResponse(
                {"error": f"not a .gguf adapter: {target}"},
                status_code=400,
            )

        # If the requested adapter isn't currently `--lora`-loaded,
        # restart llama-server so that ONLY this adapter is loaded.
        # We deliberately don't keep a growing list of preloaded LoRAs
        # in memory — that was the old behaviour, and it meant any
        # third-party `.gguf` on disk silently rode along whether the
        # user wanted it or not. The user pays one sidecar restart
        # (~3-4s) per LoRA switch, which matches the cost of switching
        # base models and is the only honest way to keep memory tight.
        if server.adapter_id_for(target) is None:
            bridge.post("working", event="LoadAdapter", title=f"加载 {target.name}")
            try:
                await server.reload_adapters([target])
            except Exception as exc:
                bridge.post("error")
                return JSONResponse({"error": str(exc)}, status_code=500)
            bridge.post("idle")
            if server.adapter_id_for(target) is None:
                return JSONResponse(
                    {"error": f"llama-server refused adapter: {target}"},
                    status_code=500,
                )

        state["current_adapter"] = target
        return {
            "ok": True,
            "adapter": str(target),
            "persona": _persona_for(target),
        }

    @app.post("/api/classify")
    def classify_endpoint(payload: dict):
        return JSONResponse(
            {"error": "/api/classify not implemented for llama.cpp backend yet"},
            status_code=501,
        )

    # ─── Updater ───────────────────────────────────────────────────────

    @app.get("/api/update-check")
    async def update_check():
        updater.local_model_path = server.model_path or _get_active_model_path()
        return await asyncio.to_thread(updater.check)

    @app.post("/api/update-apply")
    async def update_apply():
        nonlocal startup_error
        updater.local_model_path = server.model_path or _get_active_model_path()

        async def stream():
            nonlocal startup_error
            queue: asyncio.Queue = asyncio.Queue()
            sentinel = object()
            loop = asyncio.get_running_loop()

            def producer():
                try:
                    for ev in updater.apply():
                        loop.call_soon_threadsafe(queue.put_nowait, ev)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, sentinel)

            import threading as _t
            _t.Thread(target=producer, daemon=True).start()

            bridge.post("working", event="UpdateApply", title="正在更新模型")
            try:
                while True:
                    ev = await queue.get()
                    if ev is sentinel:
                        break
                    yield _sse(ev)
                    if ev.get("phase") == "complete":
                        try:
                            # Restart llama-server against the (potentially
                            # renamed) gguf so the new weights take effect
                            # without a full sidecar restart.
                            items = discover_models(extra_roots)
                            if items:
                                target = Path(items[0]["path"])
                                await server.swap_model(target)
                                updater.local_model_path = target
                                startup_error = None
                                yield _sse({"phase": "reloaded", "model": str(target)})
                        except Exception as exc:
                            startup_error = str(exc)
                            yield _sse({"phase": "reload-error", "message": str(exc)})
            finally:
                bridge.post("idle")

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ─── Chat ──────────────────────────────────────────────────────────

    @app.post("/api/warmup")
    async def warmup():
        if not server.alive:
            return JSONResponse({"ok": False, "error": "llama-server not running"}, status_code=503)
        t0 = time.time()
        try:
            await server.complete_once(prompt=" ", max_tokens=1)
            return {"ok": True, "elapsed_ms": int((time.time() - t0) * 1000)}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    def _lora_arr_for(req: ChatRequest) -> Optional[List[dict]]:
        """Compute the per-request `lora` array.

        - disable_adapter=true  → []   (force base for this request)
        - active adapter set    → [{id, scale: 1.0}]
        - no adapter active     → []   (force base)

        Sending an empty list is intentionally explicit: llama.cpp
        treats adapters omitted from a per-request `lora` list as scale
        0.0, so base chat never depends on whatever global adapter scale
        the server happened to inherit at startup.
        """
        if req.disable_adapter:
            return []
        current = state["current_adapter"]
        if not current:
            return []
        idx = server.adapter_id_for(current)
        if idx is None:
            # State got out of sync (e.g. sidecar restarted without
            # re-registering this path). Fail open to base rather than
            # 500 — the user will notice the persona is gone and can
            # re-select from Settings.
            log.warning("active adapter %s missing from llama-server index", current)
            return []
        return [{"id": idx, "scale": 1.0}]

    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        if not req.messages:
            return JSONResponse({"error": "messages is empty"}, status_code=400)

        # Debug log: record every chat request
        _sys_preview = ""
        if req.system:
            _sys_preview = req.system[:300].replace("\n", "\\n")
        _last_msg = ""
        if req.messages:
            _last_msg = str(req.messages[-1].content)[:200]
        log.info(
            "CHAT REQ | messages=%d system=%s provider=%s stream=%s | last_msg=%s",
            len(req.messages),
            _sys_preview if _sys_preview else "(none)",
            req.model_provider or "auto",
            req.stream,
            _last_msg,
        )

        lora_arr = _lora_arr_for(req)
        if req.stream:
            return StreamingResponse(
                _stream_chat_provider(
                    provider_registry, mcp_manager, bridge, req, server, state, lora_arr,
                ),
                media_type="text/event-stream",
            )
        return JSONResponse(await _blocking_chat_provider(
            provider_registry, mcp_manager, bridge, req, server, state, lora_arr,
        ))

    @app.post("/api/debug/chat")
    async def debug_chat(payload: dict):
        """Non-streaming chat with full debug output. For AI debugging via web panel.

        Body: { "message": "text", "screen": true/false, "system": "optional override" }
        Returns: { "reply": "...", "system_used": "...", "screen_context": "...", "error": null }
        """
        message = str(payload.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "message is required"}, status_code=400)

        screen_context = None
        if payload.get("screen"):
            # Auto-trigger screen observation
            try:
                async with consent_lock:
                    pass  # don't consume consent in debug mode
                b64 = await asyncio.to_thread(screen_capture)
                ocr_mode = "local"
                parsed = []
                try:
                    status = await omniparser_manager.ensure_alive()
                    if status in ("ready", "alive"):
                        async with httpx.AsyncClient(timeout=300.0) as cli:
                            pr = await cli.post(
                                "http://127.0.0.1:8000/parse/",
                                json={"base64_image": b64, "chinese_ocr": True},
                            )
                            if pr.status_code == 200:
                                data = pr.json()
                                raw = data.get("parsed_content_list") or []
                                parsed = [str(item) for item in raw] if isinstance(raw, list) else [str(raw)]
                            else:
                                parsed = ["[OmniParser error]"]
                    else:
                        parsed = [f"[OmniParser not available: {status}]"]
                except Exception as exc:
                    parsed = [f"[OmniParser failed: {exc}]"]
                if parsed:
                    # Clean up: extract just the text content from each item
                    clean_lines = []
                    for item in parsed:
                        import ast as _ast
                        try:
                            # Items may be stringified dicts — parse them
                            d = _ast.literal_eval(item) if isinstance(item, str) and item.startswith("{") else item
                            if isinstance(d, dict):
                                content = d.get("content") or ""
                                if content:
                                    clean_lines.append(str(content))
                            elif item:
                                clean_lines.append(str(item))
                        except Exception:
                            clean_lines.append(str(item))
                    if clean_lines:
                        screen_context = "【用户当前屏幕内容】\n" + "\n".join(clean_lines)
            except Exception as exc:
                log.warning("debug screen observe failed: %s", exc)

        system = payload.get("system") or screen_context or None
        sys_preview = (system or "")[:200] if system else "(none)"
        log.info("DEBUG CHAT | message=%s | system=%s | screen=%s", message[:200], sys_preview, bool(screen_context))

        req = ChatRequest(
            messages=[{"role": "user", "content": message}],
            system=system,
            stream=False,
            max_new_tokens=4096,
            temperature=0.6,
        )
        lora_arr = _lora_arr_for(req)
        try:
            result = await _blocking_chat_provider(
                provider_registry, mcp_manager, bridge, req, server, state, lora_arr,
            )
            reply = ""
            if isinstance(result, dict):
                reply = result.get("reply") or result.get("content") or result.get("text") or str(result)
            else:
                reply = str(result)
            log.info("DEBUG CHAT DONE | reply=%s", reply[:200])
            return {
                "reply": reply,
                "system_used": system,
                "screen_context": screen_context,
                "error": None,
            }
        except Exception as exc:
            log.exception("debug chat error: %s", exc)
            return JSONResponse({"error": str(exc), "reply": "", "system_used": system, "screen_context": screen_context}, status_code=500)

    @app.post("/api/state")
    def manual_state(payload: dict):
        state = str(payload.get("state") or "idle")
        bridge.post(state, event=payload.get("event"))
        return {"ok": True}

    # ── Screen observation ──────────────────────────────────────────
    VALID_CONSENT = {"deny", "once", "always"}

    @app.post("/api/screen/consent-status")
    async def set_consent(payload: dict):
        nonlocal consent_state, consent_lock
        value = payload.get("consent", "deny")
        if value not in VALID_CONSENT:
            return JSONResponse({"error": f"invalid consent: {value}"}, status_code=400)
        async with consent_lock:
            consent_state = value
        return {"current_consent": consent_state}

    @app.get("/api/screen/consent-status")
    async def get_consent():
        return {"current_consent": consent_state}

    @app.post("/api/screen/observe")
    async def observe_screen(payload: dict = {}):
        nonlocal consent_state, consent_lock
        # Debug log: record every screen observe request
        log.info(
            "SCREEN OBSERVE | ocr_mode=%s chinese_ocr=%s consent=%s",
            payload.get("ocr_mode", "local"),
            payload.get("chinese_ocr", False),
            consent_state,
        )
        # Consume "once" permission inside the lock, before any await
        async with consent_lock:
            if consent_state == "deny":
                return JSONResponse({"error": "consent denied"}, status_code=403)
            consumed = consent_state == "once"
            if consumed:
                consent_state = "deny"

        # Capture screen (always attempt this first)
        try:
            b64 = await asyncio.to_thread(screen_capture)
        except Exception as exc:
            log.exception("screen capture failed: %s", exc)
            return JSONResponse({"error": f"screen capture failed: {exc}"}, status_code=500)

        ocr_mode = payload.get("ocr_mode", "local")
        parsed = []

        if ocr_mode == "api":
            api_url = payload.get("ocr_api_url", "").strip()
            api_key = payload.get("ocr_api_key", "").strip()
            api_model = payload.get("ocr_api_model", "").strip()
            if not api_url:
                parsed.append("[OCR API not configured]")
            else:
                try:
                    headers = {"Content-Type": "application/json"}
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"
                    body = {"image": b64}
                    if api_model:
                        body["model"] = api_model
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.post(api_url, json=body, headers=headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            # Accept both list-of-strings and structured formats
                            if isinstance(data, list):
                                parsed = [str(item) for item in data]
                            elif isinstance(data, dict):
                                texts = data.get("text") or data.get("texts") or data.get("result") or data.get("content") or str(data)
                                if isinstance(texts, list):
                                    parsed = [str(t) for t in texts]
                                else:
                                    parsed = [str(texts)]
                            else:
                                parsed = [str(data)]
                        else:
                            parsed.append(f"[OCR API error: HTTP {resp.status_code}]")
                except Exception as exc:
                    log.warning("OCR API call failed: %s", exc)
                    parsed.append(f"[OCR API call failed: {exc}]")
        else:
            # Local mode: try OmniParser if running
            try:
                status = await omniparser_manager.ensure_alive()
                if status in ("ready", "alive"):
                    chinese = payload.get("chinese_ocr", False)
                    async with httpx.AsyncClient(timeout=300.0) as cli:
                        pr = await cli.post(
                            f"http://127.0.0.1:8000/parse/",
                            json={"base64_image": b64, "chinese_ocr": chinese},
                        )
                        if pr.status_code == 200:
                            data = pr.json()
                            raw = data.get("parsed_content_list") or data.get("texts") or []
                            if isinstance(raw, list):
                                parsed = [str(item) for item in raw]
                            else:
                                parsed = [str(raw)]
                        else:
                            parsed.append("[OmniParser error]")
                else:
                    parsed.append("[OmniParser not available]")
            except Exception as exc:
                log.warning("OmniParser call failed: %s: %s", type(exc).__name__, exc)
                parsed.append(f"[OmniParser call failed: {type(exc).__name__}]")

        if not parsed:
            parsed.append("[No text detected]")

        # Clean up: extract just the text content from each item
        clean_parsed = []
        for item in parsed:
            import ast as _ast
            try:
                d = _ast.literal_eval(item) if isinstance(item, str) and item.startswith("{") else item
                if isinstance(d, dict):
                    content = d.get("content") or ""
                    if content:
                        clean_parsed.append(str(content))
                elif item:
                    clean_parsed.append(str(item))
            except Exception:
                clean_parsed.append(str(item))
        if clean_parsed:
            parsed = clean_parsed

        log.info("SCREEN OBSERVE DONE | parsed=%d items, first=%s", len(parsed), parsed[0][:100] if parsed else "")

        return {
            "parsed_content_list": parsed,
            "som_image_base64": None,
            "raw_screenshot_base64": b64,
            "consent_consumed": consumed,
            "current_consent": consent_state,
            "timed_out": False,
        }

    @app.post("/api/screen/click")
    async def click_screen(payload: dict):
        nonlocal consent_state, consent_lock
        async with consent_lock:
            if consent_state == "deny":
                return JSONResponse({"error": "consent denied"}, status_code=403)

        element = payload.get("element")
        if not element:
            return JSONResponse({"error": "element is required"}, status_code=400)
        if "bbox" not in element:
            return JSONResponse({"error": "element must contain bbox"}, status_code=400)

        try:
            result = await screen_click.click_element(element)
            return result
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            log.warning("screen click failed: %s", exc)
            return JSONResponse({"error": f"screen click failed: {exc}"}, status_code=500)

    @app.get("/")
    def index():
        return JSONResponse({
            "ok": True,
            "note": "MiniCPM sidecar gateway (llama.cpp backend)",
            "endpoints": [
                "/api/health", "/api/chat", "/api/warmup",
                "/api/models", "/api/load-model", "/api/upload-model",
                "/api/devices", "/api/set-device", "/api/onboarding",
                "/api/update-check", "/api/update-apply",
                "/api/adapters", "/api/load-adapter", "/api/classify",
                "/api/state",
                "/api/skills", "/api/skills/{name}", "/api/skills/learn",
                "/api/curator/run", "/api/curator/status", "/api/curator/paused", "/api/curator/pin", "/api/curator/restore",
                "/api/providers",
                "/api/memory",
                "/api/mcp/servers", "/api/mcp/execute",
                "/api/screen/consent-status", "/api/screen/observe", "/api/debug/chat",
            ],
        })

    return app


# ── Chat plumbing ───────────────────────────────────────────────────────────


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


# ── Phase 2: Provider-based chat ──────────────────────────────────────────

MAX_TOOL_ITERATIONS = 5

# Emotion tag the LLM appends to the end of its reply. Parsed by the
# gateway and pushed to the pet via ClawdBridge so the pet can pick an
# emotion-tagged animation. Never shown to the user (renderer strips it).
_EMOTION_RE = re.compile(r"\[EMOTION:\s*([a-z_]+)\]", re.IGNORECASE)


def _sanitize(s: str) -> str:
    """Sanitize for tool name prefix (must match _sanitize_name in providers/base.py)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(s or ""))


def _build_local_provider(server, server_state, req, lora_arr=None):
    """Build a fresh LocalProvider with the pre-computed LoRA array.

    `lora_arr` comes from `_lora_arr_for(req)`, which is the single source of
    truth: it honors `disable_adapter` and returns `[]` (not None) when no
    adapter is active, so llama-server forces base output for this request.
    Recomputing it here used to silently ignore `disable_adapter`, letting a
    persona LoRA bleed into narrator replies.
    """
    from .providers.local import LocalProvider
    return LocalProvider(
        server,
        enable_thinking=bool(req.thinking),
        lora=lora_arr,
    )


def _resolve_provider(
    registry: ProviderRegistry,
    req: ChatRequest,
    server: LlamaServer,
    server_state: dict,
    log,
    lora_arr: Optional[list[dict]] = None,
) -> BaseProvider:
    """Pick the provider for this request based on model_provider / auto_route.

    Falls back to local only if explicitly requested or no API provider configured.
    Logs a clear message about which provider is being used.
    """
    from .providers.local import LocalProvider
    target = str(req.model_provider or "").strip().lower() if req.model_provider else ""

    if req.auto_route or target == "auto":
        # Auto-route: prefer any non-local provider, then fall back
        names = [p.name for p in registry._providers.values() if p.name != "local"]
        if names:
            log.info("auto-route: selected %s (available: %s)", names[0], names)
            return registry.get(names[0])
        log.info("auto-route: no API providers, using local")
        return _build_local_provider(server, server_state, req, lora_arr)

    if target and target != "local":
        if target in registry:
            log.info("provider routing: using %s (model=%s)", target, getattr(registry.get(target), '_model', '?'))
            return registry.get(target)
        # Not found - log error and fall back to local with warning in SSE
        names = [p.name for p in registry._providers.values()]
        log.warning("provider '%s' not found in registry (available: %s), falling back to local", target, names)
        # Return local so the chat still works, but the user will know via SSE

    return _build_local_provider(server, server_state, req, lora_arr)


async def _gather_tools(mcp_manager: MCPManager, req: ChatRequest) -> Optional[list[ToolDef]]:
    """Gather MCP tool definitions if tools are enabled."""
    if not req.tools_enabled:
        return None
    try:
        tools_data = await mcp_manager.list_all_tools()
        if not tools_data:
            return None
        return [ToolDef(**t) for t in tools_data]
    except Exception as exc:
        get_logger().debug("gather_tools error: %s", exc)
        return None


async def _stream_chat_provider(
    registry: ProviderRegistry,
    mcp_manager: MCPManager,
    bridge: ClawdBridge,
    req: ChatRequest,
    server: LlamaServer,
    server_state: dict,
    lora_arr: Optional[list[dict]] = None,
) -> AsyncGenerator[bytes, None]:
    """Provider-based streaming chat with MCP tool support."""
    log = get_logger()

    if not req.silent:
        bridge.new_session()
        bridge.post("thinking")

    provider = _resolve_provider(registry, req, server, server_state, log, lora_arr)

    tools_list = await _gather_tools(mcp_manager, req)

    # Build reverse mapping: prefixed_name → (server_name, tool_name)
    _tool_map: dict[str, tuple[str, str]] = {}
    if tools_list:
        for t in tools_list:
            prefixed = f"mcp_{_sanitize(t.server_name)}_{_sanitize(t.name)}"
            _tool_map[prefixed] = (t.server_name, t.name)

    # Helper: check whether the current provider+model can process image inputs.
    def _can_see_images() -> bool:
        from .providers.anthropic import AnthropicProvider
        if isinstance(provider, AnthropicProvider):
            return True  # all Claude models since 3.0 support vision
        from .providers.openai import OpenAIProvider
        if isinstance(provider, OpenAIProvider):
            model = (getattr(provider, "model", "") or "").lower()
            no_vision = ["gpt-3.5", "text-", "embedding", "davinci", "babbage"]
            return not any(nv in model for nv in no_vision)
        # Local / llama.cpp: vision needs --mmproj
        if getattr(server, "mmproj_path", None):
            return True
        return False

    yield _sse({"event": "start"})

    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    system = req.system

    cw = getattr(provider, "_context_window", None) or req.context_window
    if cw and isinstance(cw, int) and cw > 0:
        estimated = sum(len(json.dumps(m)) for m in messages) // 3
        system = (system or "") + f"\n\nThis model has a context window of {cw} tokens. Current conversation uses approximately {estimated} tokens."

    iteration = 0
    tool_occurred = False
    think_filter = ThinkBlockFilter(expose=req.thinking, start_inside=False)
    accumulated_full_text: list[str] = []
    full_text = ""

    while iteration < MAX_TOOL_ITERATIONS:
        iteration += 1
        tool_occurred = False

        if not req.silent:
            bridge.post("working")

        # Save previous iteration's text before reset
        if full_text.strip():
            accumulated_full_text.append(full_text)
        full_text = ""
        pending_tool_calls: list[dict] = []

        # Determine parameters based on provider type
        effective_max = _effective_max_new_tokens(req)
        # API providers (not local) need >=8192 for tool calls + long replies
        if provider.name != "local":
            effective_max = max(effective_max, 8192)
        chat_kwargs = {
            "messages": messages,
            "system": system,
            "tools": tools_list,
            "max_tokens": effective_max,
            "temperature": req.temperature,
            "top_p": req.top_p,
        }
        if provider.name == "local":
            chat_kwargs["top_k"] = req.top_k
            chat_kwargs["repetition_penalty"] = req.repetition_penalty

        try:
            async for event in provider.chat(**chat_kwargs):
                if event["type"] == "delta":
                    full_text += event["content"]
                    for ev in think_filter.feed(event["content"]):
                        yield _sse(ev)
                elif event["type"] == "think":
                    yield _sse({"event": "think", "content": event["content"]})
                elif event["type"] == "tool_call":
                    pending_tool_calls.append(event)
                    tool_occurred = True
                elif event["type"] == "error":
                    yield _sse({"event": "error", "message": event["message"]})
                    if not req.silent:
                        bridge.post("error")
                    return
                elif event["type"] == "end":
                    break
        except Exception as exc:
            log.exception("provider chat error: %s", exc)
            yield _sse({"event": "error", "message": str(exc)})
            if not req.silent:
                bridge.post("error")
            return

        # Check for [MCP:] markers in text output. This runs for ALL
        # providers as a fallback — cloud models (DeepSeek, Qwen API)
        # often output tool calls as text instead of using the API's
        # native function-calling mechanism.
        if not pending_tool_calls:
            mcp_calls = parse_mcp_calls(full_text)
            if mcp_calls and mcp_manager:
                tool_occurred = True
                for mc in mcp_calls:
                    pending_tool_calls.append({
                        "type": "tool_call",
                        "id": f"mcp_{mc['server_name']}_{mc['name']}",
                        "server_name": mc["server_name"],
                        "name": mc["name"],
                        "arguments": mc["arguments"],
                    })

        if not tool_occurred:
            break

        # Execute pending tool calls
        for tc in pending_tool_calls:
            tn = tc.get("name", "")
            if not tn:
                continue
            # Resolve server name from tool name map
            sn = tc.get("server_name", "default")
            if tn in _tool_map:
                sn, tn = _tool_map[tn]
            elif sn == "default":
                # Try to find matching tool across all servers
                log.warning("Unresolved tool call: %s, trying call_tool_by_name", tn)
            args = tc.get("arguments", {})
            tc_id = tc.get("id", "")

            log.info("MCP tool call: %s/%s args=%s", sn, tn, args)

            result = await mcp_manager.call_tool(sn, tn, args)
            summary = result.get("summary", f"[Tool {tn} completed]")

            # Add to message context for next iteration
            if provider.supports_function_calling and tc_id:
                is_anthropic = isinstance(provider, AnthropicProvider)
                if is_anthropic:
                    prefixed = f"mcp_{_sanitize(sn)}_{_sanitize(tn)}"
                    messages.append({
                        "role": "assistant",
                        "content": [{
                            "type": "tool_use",
                            "id": tc_id,
                            "name": prefixed,
                            "input": args,
                        }],
                    })
                else:
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": tn, "arguments": json.dumps(args)},
                        }],
                    })
            else:
                messages.append({"role": "assistant", "content": f"[{tn}]"})
            # Build tool result content — multimodal if provider can see images
            screenshot_b64 = result.get("screenshot_base64")
            if screenshot_b64 and _can_see_images():
                # For local providers (no function calling), use 'user' role
                # because llama-server doesn't process images in 'tool' role
                # messages. Anthropic/OpenAI handle tool role images natively.
                if provider.supports_function_calling:
                    tool_content = [
                        {"type": "text", "text": summary},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
                    ]
                    messages.append({
                        "role": "tool",
                        "content": tool_content,
                        "tool_call_id": tc_id or f"call_{tn}",
                        "name": tn,
                    })
                else:
                    # Local model: inject as a user message with image content
                    messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"[{tn} 结果]\n{summary}"},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
                        ],
                    })
            else:
                tool_content = summary
                messages.append({
                    "role": "tool",
                    "content": tool_content,
                    "tool_call_id": tc_id or f"call_{tn}",
                    "name": tn,
                })

            # Brief notification to client
            yield _sse({
                "event": "delta",
                "content": f"\n_执行 {tn}..._\n",
            })

    for ev in think_filter.flush():
        yield _sse(ev)

    # ── Emotion tag extraction ──
    # The LLM is instructed (via system prompt) to end replies with
    # [EMOTION:happy] etc. Parse it from the accumulated text across all
    # tool-calling iterations and push the emotion to the pet via the
    # existing ClawdBridge state path. The renderer strips the tag from
    # the visible text separately.
    # Use the LAST emotion tag in the reply — small models sometimes emit
    # an intermediate tag (e.g. [EMOTION:neutral]) before settling on a
    # final one. Also tolerate whitespace after the colon. The renderer
    # sends the emotion/next_chat instruction to ALL providers (remote
    # and local) via body.system, so we parse for every provider.
    if full_text.strip():
        accumulated_full_text.append(full_text)
    combined_text = " ".join(accumulated_full_text) if accumulated_full_text else full_text

    _emotion_matches = _EMOTION_RE.findall(combined_text)
    _emotion = _emotion_matches[-1].lower() if _emotion_matches else "neutral"
    log.info("emotion parsed: %s (all_matches=%s)", _emotion, _emotion_matches)

    # ── Proactive chat scheduling ──
    # Proactive chat: model outputs [NEXT_CHAT:N] where N is integer seconds.
    # 0 means "no proactive chat". Sent via SSE so renderer can setTimeout.
    _next_chat_re = re.compile(r"\[NEXT_CHAT:\s*(\d+)\s*(?:s|sec|seconds)?\s*\]", re.IGNORECASE)
    _nc_match = _next_chat_re.search(combined_text)
    if _nc_match:
        _nc_seconds = max(0, int(_nc_match.group(1)))
        yield _sse({"event": "next_chat", "seconds": _nc_seconds})

    if not req.silent:
        bridge.post("attention", emotion=_emotion)
    yield _sse({"event": "end"})


async def _blocking_chat_provider(
    registry: ProviderRegistry,
    mcp_manager: MCPManager,
    bridge: ClawdBridge,
    req: ChatRequest,
    server: LlamaServer,
    server_state: dict,
    lora_arr: Optional[list[dict]] = None,
) -> dict:
    """Non-streaming provider-based chat. Accumulates the stream into a response."""
    collector: list[str] = []
    async for sse_bytes in _stream_chat_provider(
        registry, mcp_manager, bridge, req, server, server_state, lora_arr,
    ):
        # Parse SSE to collect content
        line = sse_bytes.decode("utf-8", "ignore")
        if line.startswith("data: "):
            import json as _json
            try:
                obj = _json.loads(line[6:].strip())
                if obj.get("event") == "delta":
                    collector.append(obj.get("content", ""))
            except _json.JSONDecodeError:
                pass
    return {"content": "".join(collector), "thinking": None}
