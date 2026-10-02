"""FastAPI gateway in front of llama.cpp's llama-server.

Exposes the same HTTP/SSE contract the Electron app already speaks with
the legacy PyTorch sidecar, so the renderer (clawd-on-desk/src/pet-chat.*)
does not need to change. The actual inference happens in the subprocess
owned by `LlamaServer`; this file is just glue.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import platform
import re
import shlex
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
from .memory import MemoryStore, EventStore, MoodStore
from .memory.events import parse_event_block, promote_core
from .memory.tool import (
    MEMORY_TOOL_SCHEMA,
    memory_tool_handler,
    set_memory_store,
)
from .memory import impulse as _impulse_module
from .memory import recall as _recall_module
from .memory import loader as _loader_module
from .memory import continuity as _continuity_module
from .memory import resonance as _resonance_module
from .memory import somatic as _somatic_module
from . import petplugins as _petplugins_module
from . import plugin_policy as _plugin_policy

# Kernel container mirror: _stream_chat_provider is a MODULE-level function
# and cannot see build_app's closure, so the live PluginManager is mirrored
# here at boot. _organ_service() resolves an organ service through it and
# falls back to the built-in module when the organ is off or absent.
_plugin_container_ref: dict = {}


def _organ_service(name: str, fallback):
    try:
        _mgr = _plugin_container_ref.get("mgr")
        _svc = _mgr.get_service(name) if _mgr else None
    except Exception:
        _svc = None
    return _svc or fallback


def _kernel():
    """Module-level access to the plugin container (None before boot)."""
    return _plugin_container_ref.get("mgr")


# Where the chat pipeline swallows an exception. The file logger runs at INFO,
# so a log.debug() inside `except Exception` is indistinguishable from "never
# happened": the model went a week without seeing a single plugin tool because
# the only trace was a debug line. Count instead, and surface the count.
_silent_failures: dict[str, int] = {}


def _count_failure(site: str) -> int:
    _silent_failures[site] = _silent_failures.get(site, 0) + 1
    return _silent_failures[site]


def _note_failure(site: str, exc: BaseException) -> None:
    """Count a swallowed error and log it at a level that actually reaches disk.

    Throttled to the first occurrence and every 20th: these sit on the chat
    hot path, and an MCP server that stays down would otherwise repeat one
    line per turn forever.
    """
    n = _count_failure(site)
    if n == 1 or n % 20 == 0:
        get_logger().warning("swallowed error at %s (x%d): %s", site, n, exc)

from .memory.recall import (
    RECALL_TOOL_SCHEMA,
    recall_tool_handler,
    get_event_store,
    get_session_recall_count,
    set_event_store,
)
from .screen_consent import ScreenPermissionManager
from .holo_agent import HoloRunner, probe as holo_probe
from .memory_context import MemoryContext, theme_slug
from .memory.mood import build_mood_assessment_prompt, EMOTION_TO_MOOD

# Timestamp of the most recent conversation — the decay organ freezes when
# the user hasn't talked to the pet for FREEZE_HOURS (memory only fades
# while the pet is awake). Updated at the start of each /api/chat stream.
# Shared as a cell (not a bare global) because it is also a KERNEL SERVICE:
# the organ_decay / organ_dream plugins inject "conversation_state" and read
# this dict, so the chat path and the organs see the same truth.
_conversation_state = {"last_conversation_at": None}

# Thread-safe submit of coroutines onto the main asyncio loop. Plugins load
# (and hot-reload) from the watcher thread, and the provider clients are
# loop-bound — so organ plugins that need async submit through this service
# instead of spawning their own loop.
_main_loop_holder: dict = {}


def _schedule_on_main_loop(coro):
    loop = _main_loop_holder.get("loop")
    if loop is None or loop.is_closed():
        coro.close()
        return None
    return loop.call_soon_threadsafe(loop.create_task, coro)

# Death-note hook (死亡遗嘱): set by build_app so the module-level chat
# stream can persist memories even when the client dies mid-stream —
# the renderer is the usual extraction trigger, but the renderer is
# also the thing most likely to vanish. Signature: fn(reply_text) -> dict.
_extraction_hook = None

# Consecutive proactive requests immediately preceding the current one
# (a proactive request = the last user message is the renderer's
# [系统提示：…] wake-up prompt). Feeds the speech-impulse backoff: the
# longer the pet has been talking into the void, the longer the
# subconscious waits before trying again. Reset the moment the user
# actually speaks.
_proactive_streak = 0

# Marker the renderer uses for proactive wake-up prompts ([系统提示：…]).
_PROACTIVE_PROMPT_PREFIX = "[系统提示"

# ── 身体感受 (somatic sense) ─────────────────────────────────────────────
# The pet BODY (Electron shell) reports touch: drags, click bursts. One
# process-wide buffer shared by the reporting endpoint (writer) and the
# chat stream (reader) — module scope because _stream_chat_provider sits
# outside build_app's closure (the event_store NameError lesson).
_somatic_buffer = _somatic_module.SensationBuffer()
from . import screen_click
from .screen_capture import capture as screen_capture
from .think_filter import ThinkBlockFilter, ControlTagFilter, ToolNarrationFilter
from .updater import DEFAULT_SOURCE as DEFAULT_UPDATE_SOURCE
from .updater import ModelUpdater
from .sidecar_updater import SidecarUpdater


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
        description="'local' | 'openai' | 'anthropic' | None (=local fallback)",
    )
    tools_enabled: bool = Field(
        default=True,
        description="When false, skip MCP tool injection and detection",
    )
    context_window: Optional[int] = None


class PetInteractionRequest(BaseModel):
    """One body interaction reported by the Electron shell (the BODY).

    kind="drag": the user picked the pet up and moved it — distance_px is
    the straight-line window displacement, duration_ms the gesture length.
    kind="click": a settled click burst — clicks is the burst count
    (1 = a tap, 2-3 = poking, 4+ = relentless poking).
    """
    kind: str
    clicks: int = 0
    distance_px: int = 0
    duration_ms: int = 0


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


# ── Emotion index → temperature (情绪指数温度调控) ─────────────────────
# The pet's emotion index (calm=0, happy +0.1, sad -0.1, …) modulates the
# generation temperature: happy → more divergent/creative, sad → more
# subdued. Pure function so it's unit-testable.
TEMP_BASE_MOD = 0.3
TEMP_MIN = 0.2
TEMP_MAX = 1.5


def modulate_temperature(base: float, emotion_index: float) -> float:
    """base + index*0.3, clamped to [0.2, 1.5]. index=0 → base unchanged."""
    return max(TEMP_MIN, min(TEMP_MAX, float(base) + float(emotion_index) * TEMP_BASE_MOD))


# ── Model discovery ─────────────────────────────────────────────────────────


def _is_mmproj(name: str) -> bool:
    """mmproj-*.gguf files are vision projector weights, NOT main models.
    Loading one as --model fails ("unsupported model architecture:
    'clip'"), so they must never surface in model discovery."""
    return name.lower().startswith("mmproj")


def discover_models(roots: List[Path]) -> List[dict]:
    """Return [{name, path}] for every *.gguf file under `roots`.

    Excludes mmproj-* vision-projector files (they are --mmproj weights,
    not loadable as a main model).
    """
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
        if r.is_file() and r.suffix.lower() == ".gguf" and not _is_mmproj(r.name):
            out.append({"name": r.name, "path": str(r)})
            continue
        if not r.is_dir():
            continue
        for p in sorted(r.rglob("*.gguf")):
            if any(part.endswith(".update-staging") or part.endswith(".bak") for part in p.parts):
                continue
            if _is_mmproj(p.name):
                continue
            out.append({"name": p.name, "path": str(p)})
    return out


def _default_model_roots() -> List[Path]:
    """Locations to scan for *.gguf when no explicit PET_MODEL_DIR
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
    """Where to scan for `*.gguf` LoRA adapters when no `PET_ADAPTER_DIR`
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

    1. `PET_ADAPTER_DIR` env (Electron host injects this in packaged
       mode pointing at `<userData>/adapters/`)
    2. First default root that already exists
    3. First default root regardless of existence (the caller can then
       `mkdir -p` before opening Finder)
    """
    env_dir = os.environ.get("PET_ADAPTER_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    for cand in _default_adapter_roots():
        if cand.exists() and cand.is_dir():
            return cand
    defaults = _default_adapter_roots()
    return defaults[-1] if defaults else None


# Mirror file Electron writes after every manifest mutation. Lives in
# the adapter dir under a dot prefix so `discover_adapters`'s `*.gguf`
# scan misses it. Schema mirrors `<userData>/pet-adapters.json` 1:1
# (see clawd-on-desk/src/pet-chat.js).
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
    # host has persisted as the active one (env PET_ACTIVE_ADAPTER)
    # gets passed to llama-server via --lora. Default behaviour is pure
    # Base — no third-party LoRA is preloaded just because it happens
    # to live on disk. Switching to a different LoRA later triggers
    # `LlamaServer.reload_adapters([new])`, costing one llama-server
    # restart but keeping the steady-state memory minimal.
    _env_active = os.environ.get("PET_ACTIVE_ADAPTER", "").strip()
    initial_active: Optional[Path] = None
    if _env_active:
        try:
            cand = Path(_env_active).expanduser().resolve(strict=True)
            if cand.suffix.lower() == ".gguf":
                initial_active = cand
            else:
                log.warning("PET_ACTIVE_ADAPTER ignored (not .gguf): %s", cand)
        except FileNotFoundError:
            log.warning("PET_ACTIVE_ADAPTER points at missing file: %s", _env_active)

    def _env_int(name: str) -> Optional[int]:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            log.warning("%s=%r is not an int; ignoring", name, raw)
            return None

    def _env_bool(name: str) -> Optional[bool]:
        raw = (os.environ.get(name) or "").strip().lower()
        if raw in ("1", "true", "on", "yes"):
            return True
        if raw in ("0", "false", "off", "no"):
            return False
        return None

    server = LlamaServer(
        model_path=initial_model,
        ctx_size=ctx_size,
        n_gpu_layers=n_gpu_layers,
        threads=threads,
        adapters=[initial_active] if initial_active else [],
        # Boot-time multimodal pairing: picked up if the bundled model
        # ships with a corresponding `mmproj-*.gguf` in the same dir.
        mmproj_path=find_sibling_mmproj(initial_model) if initial_model else None,
        # Advanced runtime knobs, injected by the Electron host from
        # Settings → Engine → Advanced parameters. Defaults keep the
        # engine's own behaviour except load-mode, which we pin to mmap
        # (a 20 GB+ checkpoint must not be read into RAM wholesale).
        load_mode=(os.environ.get("PET_LOAD_MODE") or "mmap").strip() or "mmap",
        cache_type_k=(os.environ.get("PET_CACHE_TYPE_K") or "").strip() or None,
        cache_type_v=(os.environ.get("PET_CACHE_TYPE_V") or "").strip() or None,
        flash_attn=_env_bool("PET_FLASH_ATTN"),
        n_cpu_moe=_env_int("PET_N_CPU_MOE"),
        batch_size=_env_int("PET_BATCH_SIZE"),
        ubatch_size=_env_int("PET_UBATCH_SIZE"),
        parallel=_env_int("PET_PARALLEL"),
        ctx_checkpoints=_env_int("PET_CTX_CHECKPOINTS"),
        kv_offload=_env_bool("PET_KV_OFFLOAD"),
        kv_unified=_env_bool("PET_KV_UNIFIED"),
        spec_type=(os.environ.get("PET_SPEC_TYPE") or "").strip() or None,
        spec_draft_n_max=_env_int("PET_SPEC_DRAFT_N_MAX"),
        verbosity=_env_int("PET_VERBOSITY"),
        # Free-form pass-through: PET_EXTRA_ARGS="--main-gpu 0 --tensor-split 1,0"
        # (space separated; quoted segments survive).
        extra_args=shlex.split(os.environ.get("PET_EXTRA_ARGS") or ""),
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
    # Boot consent from the Electron host (PET_SCREEN_CONSENT):
    # "always" = full permission (no per-action prompts), "deny" (default)
    # = AI-agent style per-action authorization dialogs.
    _boot_consent = os.environ.get("PET_SCREEN_CONSENT", "deny")
    consent_state: str = _boot_consent if _boot_consent in ("deny", "once", "always") else "deny"
    consent_lock = asyncio.Lock()
    omniparser_manager = OmniParserManager()

    # ── Per-action screen permission (AI-agent style) ────────────────
    # When consent is "deny", a tool wanting screen access posts a
    # permission request to the Electron host (bridge notification) and
    # awaits the user's Yes/No via /api/screen/permission-respond.
    permission_manager = ScreenPermissionManager(consent_state)

    def _notify_permission(request_id: str, tool_name: str, description: str) -> None:
        bridge.post(
            "notification",
            event="DeskPetPermission",
            title=f"桌宠想{tool_name}",
            extra={
                "request_id": request_id,
                "tool": tool_name,
                "description": description,
            },
        )

    async def _request_screen_permission(tool_name: str, description: str) -> bool:
        """Ask the user (via the Electron host) to authorize one screen
        action. Returns True when allowed, False on deny / timeout."""
        return await permission_manager.request(
            _notify_permission, tool_name, description
        )

    # ── Long-term memory store ───────────────────────────────────────
    # One store per sidecar process. The frozen snapshot (MEMORY.md +
    # USER.md) is captured here at boot and served to the system prompt
    # via /api/memory. Mid-session tool writes persist to disk immediately
    # but do NOT mutate the snapshot — see memory/store.py for the rationale
    # (prefix-cache preservation across turns).
    memory_dir_env = os.environ.get("PET_MEMORY_DIR", "").strip()
    if memory_dir_env:
        memory_dir = Path(memory_dir_env).expanduser()
    else:
        # Dev fallback when the Electron host didn't inject the env (direct
        # CLI run). MUST match the Electron userData layout — app name is
        # "deskpt" (clawd-on-desk/package.json), and Electron's
        # app.getPath("userData") is %APPDATA%\<name>. A mismatch here
        # splits the user's memories into two parallel worlds depending on
        # whether the sidecar was launched by the shell or by Electron, so
        # this fallback mirrors that name AND logs loudly.
        _app_dir = "deskpt"
        if platform.system() == "Darwin":
            memory_dir = Path.home() / "Library" / "Application Support" / _app_dir / "memories"
        elif platform.system() == "Windows":
            memory_dir = Path.home() / "AppData" / "Roaming" / _app_dir / "memories"
        else:
            memory_dir = Path.home() / ".local" / "share" / _app_dir / "memories"
        log.warning(
            "PET_MEMORY_DIR not injected — using dev fallback %s. If this "
            "sidecar was started by Electron, pet-chat.js failed to pass the "
            "env and the pet would read/write a DIFFERENT memory root than "
            "the user's real one; check the launcher.",
            memory_dir,
        )
    # ── Theme-scoped memory (持续自我存在) ─────────────────────────
    # One MemoryContext holds the soul-layer stores (identity / episodic
    # / mood) and switches them per animation theme. The boot theme comes
    # from the Electron host via PET_THEME.
    mem_ctx = MemoryContext(memory_dir)
    boot_theme = os.environ.get("PET_THEME", "default")
    try:
        mem_ctx.switch(boot_theme)
        log.info("memory context: theme=%s base=%s", mem_ctx.current_theme, memory_dir)
    except Exception as exc:
        log.warning("memory context boot failed (continuing): %s", exc)
    memory_store = mem_ctx.memory_store
    event_store = mem_ctx.event_store
    mood_store = mem_ctx.mood_store

    # ── Cordis-style plugin container (万物皆插件 → 自进化) ──────────
    # One process-wide container (module scope: the chat stream reads it
    # outside build_app's closure — same reason as _somatic_buffer).
    # Services = the soul-layer stores; plugins inject what they need.
    # Registered THROUGH the kernel rather than by writing the dict, so
    # injecting plugins get a coeffect notification: a plugin injected on
    # "mood" parks at boot and activates the instant mood lands.
    _pet_plugins = _petplugins_module.PluginManager({})
    try:
        _main_loop_holder["loop"] = asyncio.get_running_loop()
    except RuntimeError:
        pass  # not inside a running loop (unit tests) — schedule stays inert
    for _svc_name, _svc_obj in (
        ("mood", mood_store),
        ("events", event_store),
        ("memory", memory_store),
        ("memory_dir", memory_dir),
        ("memory_ctx", mem_ctx),
        ("llama_server", server),
        ("schedule", _schedule_on_main_loop),
        ("conversation_state", _conversation_state),
    ):
        _pet_plugins.register_service(_svc_name, _svc_obj, owner="kernel")
    # gateway service = the container itself (plugins can introspect peers)
    _pet_plugins.register_service("gateway", _pet_plugins, owner="kernel")
    _plugin_container_ref["mgr"] = _pet_plugins  # module-level mirror
    try:
        _sync = _pet_plugins.sync()
        if _sync["loaded"] or _sync["failed"]:
            log.info("pet plugins: loaded=%s failed=%s", _sync["loaded"], _sync["failed"])
        _pet_plugins.start_watching()
    except Exception as exc:
        log.warning("pet plugins boot failed (continuing): %s", exc)

    # ── plugin_forge: the self-evolution organ (CORE, always loaded) ──
    # The model authors its own plugins through one tool call; the hot
    # loader brings them alive within SCAN_INTERVAL_SECONDS.
    def _forge_ctx() -> _petplugins_module.PluginContext:
        ctx = _petplugins_module.PluginContext("plugin_forge", _pet_plugins)
        def forge(args: dict) -> str:
            name = "".join(ch for ch in str(args.get("name", "")) if ch.isalnum() or ch == "_").strip()
            code = str(args.get("code", ""))
            if not name or not code:
                return "[forge error: need 'name' and 'code']"
            if "\x00" in code or len(code) > 64_000:
                return "[forge error: invalid or oversized code]"
            # Audit V-1: refuse dangerous code BEFORE it reaches the plugins
            # directory, and answer with the reason so the model can rewrite
            # instead of wondering why its organ never showed up.
            violations = _plugin_policy.check_source(code)
            if violations:
                return (
                    "[forge refused: 这段代码没有通过插件安全策略，未写入]\n"
                    + _plugin_policy.format_violations(violations)
                    + "\n可用范围：只能 import gateway 与少量纯计算标准库"
                    "（datetime/json/re/math/time/asyncio/threading/types 等），"
                    "不能碰文件系统、进程、网络，也不能用 eval/exec/open/getattr(动态名)。"
                    "需要这些能力请调用已注入的服务（ctx.tool / ctx.require）。"
                )
            pdir = _petplugins_module.PluginManager.plugin_dir(memory_dir)
            pdir.mkdir(parents=True, exist_ok=True)
            target = pdir / f"{name}.py"
            # Rollback insurance (cordis temporal composability applied to
            # self-evolution): keep the previous revision so a bad rewrite
            # cannot destroy a working organ. That is the difference
            # between "the pet tried to evolve" and "the pet bricked
            # itself while trying".
            previous = target.read_text(encoding="utf-8") if target.exists() else None
            target.write_text(code, encoding="utf-8")
            # sync immediately — the plugin is alive when the tool returns
            result = _pet_plugins.sync()
            if name in result["loaded"]:
                tools = [
                    t for c in _pet_plugins.describe()
                    if c["name"] == name for t in c["tools"]
                ]
                return f"插件 '{name}' 已铸造并加载。可用工具: {tools or '（无）'}"
            if name in result["skipped"]:
                waiting = [
                    p for p in _pet_plugins.describe_pending() if p["name"] == name
                ]
                missing = waiting[0]["missing"] if waiting else ["?"]
                return (
                    f"插件 '{name}' 已写入，正在等待服务 {missing} —— "
                    f"服务出现后会自动激活，不必重写。"
                )
            # failed → restore the last working revision
            if previous is not None:
                target.write_text(previous, encoding="utf-8")
                _pet_plugins.sync()
                return f"插件 '{name}' 加载失败，已回滚到上一可用版本（旧器官仍在工作）。"
            target.unlink(missing_ok=True)
            _pet_plugins.sync()
            return f"插件 '{name}' 加载失败（无可回滚的旧版本），文件已移除。"
        def unforge(args: dict) -> str:
            name = str(args.get("name", "")).strip()
            pdir = _petplugins_module.PluginManager.plugin_dir(memory_dir)
            target = pdir / f"{name}.py"
            if target.is_file():
                target.unlink()
                _pet_plugins.sync()
                return f"插件 '{name}' 已卸载并删除。"
            return f"插件 '{name}' 不存在。"
        def roster(args: dict) -> str:
            plugins = _pet_plugins.describe()
            if not plugins:
                return "当前没有任何插件。"
            return "\n".join(f"- {p['name']}: tools={p['tools']}" for p in plugins)
        ctx.tool(
            "pet_forge_plugin",
            "铸造一个新插件（Python，定义 apply(ctx)，用 ctx.tool() 注册能力）并热加载。这是你给自己长新器官的方式。",
            {"type": "object", "properties": {"name": {"type": "string"}, "code": {"type": "string"}}, "required": ["name", "code"]},
            forge,
        )
        ctx.tool(
            "pet_unload_plugin",
            "卸载并删除一个插件（放弃一个自己长出的器官）。",
            {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            unforge,
        )
        ctx.tool(
            "pet_list_plugins",
            "列出当前所有插件及其提供的工具。",
            {"type": "object", "properties": {}},
            roster,
        )
        return ctx

    _pet_plugins._contexts["plugin_forge"] = _forge_ctx()
    _pet_plugins.protect("plugin_forge")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal startup_error
        # Don't fail boot when the model isn't on disk yet — onboarding
        # downloads it via /api/update-apply and only then calls
        # /api/load-model. The pet still wants /api/health to answer 200
        # in the meantime so the bubble doesn't show a permanent error.
        if not _engine_enabled():
            # The user stopped the engine. Restarting the pet must not
            # resurrect it - that is the "I turned it off and it came back"
            # complaint. Wait for an explicit /api/engine/start.
            log.info(
                "built-in engine disabled by the user; not starting "
                "(call /api/engine/start to re-enable)"
            )
        elif initial_model and Path(initial_model).exists():
            try:
                await server.start()
                startup_error = None
            except Exception as exc:
                startup_error = str(exc)
                log.exception("initial llama-server start failed: %s", exc)
        else:
            log.info("model not present at startup; waiting for /api/load-model")
        bridge.post("idle", title="DeskPet 桌宠")
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

        # ── B-15: self-check heartbeat (自主稳态——检测"我病了"并记下来) │
        # Passive survival exists (web-panel lifeboat, provider fallback,
        # OmniParser restart cap). What's missing is ACTIVE self-awareness:
        # periodically probe each organ, count consecutive failures, and on
        # a state transition write an event memory — the pet remembers being
        # sick, and wakes up knowing it was sick ("生病了"叙事进记忆流).
        async def _health_loop():
            HEALTH_INTERVAL = 300          # seconds between probes
            SICK_THRESHOLD = 3             # consecutive failures → degraded
            state = {"llama_fail": 0, "bridge_fail": 0, "degraded": False}
            while True:
                try:
                    await asyncio.sleep(HEALTH_INTERVAL)
                    # Organ 1: llama-server alive?
                    if getattr(server, "alive", False):
                        state["llama_fail"] = 0
                    else:
                        # /api/load-model not yet called is normal (onboarding)
                        if initial_model or getattr(server, "started_once", False):
                            state["llama_fail"] = state["llama_fail"] + 1
                    # Organ 2: bridge to the pet body reachable?
                    try:
                        bridge_ok = bool(getattr(bridge, "is_reachable", lambda: True)())
                    except Exception:
                        bridge_ok = False
                    state["bridge_fail"] = 0 if bridge_ok else state["bridge_fail"] + 1

                    sick = state["llama_fail"] >= SICK_THRESHOLD or state["bridge_fail"] >= SICK_THRESHOLD
                    if sick and not state["degraded"]:
                        state["degraded"] = True
                        log.warning("homeostasis: DEGRADED (llama_fail=%d bridge_fail=%d)",
                                    state["llama_fail"], state["bridge_fail"])
                        try:
                            event_store = get_event_store()
                            if event_store is not None:
                                event_store.add_event(
                                title="生病了",
                                content=(
                                    f"系统自检发现自己不太对劲（"
                                    f"llama连续{state['llama_fail']}次异常，"
                                    f"桥连续{state['bridge_fail']}次异常）。"
                                    f"说话可能断断续续的。"
                                ),
                                weight=400,
                                type_="experience",
                                resolved=False,
                            )
                        except Exception as exc:
                            log.warning("homeostasis: sick-note write failed: %s", exc)
                    elif not sick and state["degraded"]:
                        state["degraded"] = False
                        log.info("homeostasis: recovered")
                        try:
                            event_store = get_event_store()
                            if event_store is not None:
                                event_store.add_event(
                                title="病好了",
                                content="系统自检恢复正常，身体舒服了。",
                                weight=300,
                                type_="experience",
                                resolved=True,
                            )
                        except Exception as exc:
                            log.warning("homeostasis: recovery-note write failed: %s", exc)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    log.warning("health loop error: %s", exc)
        _health_task = _aio.get_event_loop().create_task(_health_loop())
        try:
            yield
        finally:
            bridge.post("sleeping")
            # Organs are plugins now — unwind the container instead of
            # cancelling individual tasks (their disposers stop threads
            # and cancel scheduled tasks in reverse order).
            _pet_plugins.shutdown()
            _health_task.cancel()
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

    app = FastAPI(title="DeskPet Sidecar Gateway", lifespan=lifespan)
    # ── B-5: localhost token auth ──────────────────────────────────────
    # A pet gateway binding 127.0.0.1 with zero auth is still reachable
    # from ANY webpage running in a browser on this machine (browsers
    # happily hit localhost). Threat model: a webpage calls
    # /api/screen/click to click the user's real mouse. Fix: every
    # request must carry the X-DeskPet-Token header. The Electron host
    # learns the token from the token file in the app's userData dir;
    # the web-panel prompt for it once and stashes it in localStorage.
    _token_path = Path.home() / ".pet" / "gateway-token"
    _token_path.parent.mkdir(parents=True, exist_ok=True)
    if _token_path.is_file():
        _gateway_token = _token_path.read_text(encoding="utf-8").strip()
    else:
        import secrets as _sec
        _gateway_token = _sec.token_urlsafe(32)
        _token_path.write_text(_gateway_token, encoding="utf-8")
        try:
            os.chmod(_token_path, 0o600)  # owner-only on POSIX, no-op on Win
        except OSError:
            pass

    @app.middleware("http")
    async def _token_gate(request, call_next):
        # /api/health stays open — onboarding pings it before the host has
        # had a chance to read the token file.
        if request.url.path == "/api/health":
            return await call_next(request)
        # Constant-time compare: the token is the only gate on every
        # non-health endpoint, so it must not leak its length/prefix by
        # timing (audit V-6).
        provided = (request.headers.get("x-pet-token") or "").encode("utf-8")
        expected = _gateway_token.encode("utf-8")
        if not hmac.compare_digest(provided, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        # Web panel only — the Electron renderer uses a custom scheme so
        # CORS doesn't apply there, but we still list the known origins
        # instead of "*". "http://tauri.localhost" is the Tauri rewrite's
        # WebView2 origin (default port 80) — it talks to /api/* directly.
        allow_origins=[
            "http://127.0.0.1:18999",
            "http://localhost:18999",
            "http://tauri.localhost",
            "app://.",
            "file://",
            "null",
        ],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # Register skill routes so /api/skills endpoints are available
    # immediately (even before a model is loaded).
    skill_service = register_skill_routes(app)

    # ─── Provider registry + MCP (Phase 2) ─────────────────────────
    provider_registry = ProviderRegistry()
    # Local provider is always available
    provider_registry.register(LocalProvider(server, enable_thinking=False))
    # The dream organ (and anything else) can now activate — this was the
    # last service it was parked on.
    _pet_plugins.register_service("providers", provider_registry, owner="kernel")

    # Load API providers from ~/.pet/providers.json
    _providers_path = Path.home() / ".pet" / "providers.json"

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
                # AI-agent style: ask the user (Yes/No bubble on the
                # Electron side) for THIS action instead of refusing.
                if not await _request_screen_permission("查看屏幕", "观察屏幕内容（OmniParser OCR）"):
                    return {
                        "content": [{"type": "text", "text": (
                            "用户拒绝了这次屏幕观察。不要继续尝试，除非用户明确要求。"
                        )}],
                        "is_error": True,
                        "summary": "Screen observation denied by user.",
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
                # AI-agent style: ask the user for THIS screenshot.
                if not await _request_screen_permission("截图查看", "截取当前屏幕并查看画面"):
                    return {
                        "content": [{"type": "text", "text": (
                            "用户拒绝了这次截图。不要继续尝试，除非用户明确要求。"
                        )}],
                        "is_error": True,
                        "summary": "Screen capture denied by user.",
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

    # ── Builtin tool: screen_click (now consent-gated, async) ─────────
    async def _handle_screen_click(args):
        """MCP tool: perform a mouse action. Consent-gated like the other
        screen tools (deny → per-action user authorization)."""
        nonlocal consent_state, consent_lock
        async with consent_lock:
            if consent_state == "deny":
                if not await _request_screen_permission("操作屏幕", "在屏幕上执行鼠标操作"):
                    return {
                        "content": [{"type": "text", "text": (
                            "用户拒绝了这次屏幕操作。不要继续尝试，除非用户明确要求。"
                        )}],
                        "is_error": True,
                        "summary": "Screen click denied by user.",
                    }
            if consent_state == "once":
                consent_state = "always"
        element = args.get("element", {}) if isinstance(args, dict) else {}
        return screen_click.click_element(element)

    mcp_manager = MCPManager()
    # ── Holo GUI desktop control (the pet's hands) ───────────────────
    # The pet's language model hands a HIGH-LEVEL task to Holo (vision
    # GUI agent); Holo drives the real mouse/keyboard step by step.
    # Consent model: requesting a task consumes/asks screen consent ONCE
    # at task level — the user approves "让凌凌操作电脑：xxx", not every click.
    holo_runner = HoloRunner()

    async def _handle_holo_desktop_task(args):
        nonlocal consent_state, consent_lock
        task = str((args or {}).get("task", "")).strip()
        if not task:
            return {
                "content": [{"type": "text", "text": "缺少 task 参数。"}],
                "is_error": True,
                "summary": "Missing task.",
            }
        max_steps = int((args or {}).get("max_steps", 18) or 18)
        max_steps = max(1, min(max_steps, 40))
        if not holo_runner.configured():
            return {
                "content": [{"type": "text", "text": (
                    "桌面操作能力未配置（缺 PET_HOLO_API_KEY）。"
                    "请用户在 设置 → DeskPet 中填写 Holo API Key 后重试。"
                )}],
                "is_error": True,
                "summary": "Holo API key not configured.",
            }
        async with consent_lock:
            if consent_state == "deny":
                if not await _request_screen_permission("操作电脑", f"凌凌想要操作你的电脑完成任务：{task[:80]}"):
                    return {
                        "content": [{"type": "text", "text": "用户拒绝了这次电脑操作授权。不要继续尝试，除非用户明确要求。"}],
                        "is_error": True,
                        "summary": "Desktop control denied by user.",
                    }
            if consent_state == "once":
                consent_state = "always"

        async def _on_done(ok: bool, summary: str):
            # Tell the pet body about the outcome so it visibly reacts.
            try:
                bridge.post("success" if ok else "idle", title=f"电脑任务{'完成' if ok else '失败'}")
            except Exception:
                pass

        ack = await holo_runner.run_background(task, max_steps=max_steps, on_done=_on_done)
        if not ack.get("ok"):
            return {
                "content": [{"type": "text", "text": f"无法启动桌面任务：{ack.get('error')}"}],
                "is_error": True,
                "summary": f"Could not start: {ack.get('error')}",
            }
        return {
            "content": [{"type": "text", "text": (
                f"✋ 桌面任务已启动（最多 {max_steps} 步）。任务在后台执行，"
                "告诉我：你可以用 holo_status 查询进度，不要假装任务已完成。"
            )}],
            "summary": f"Desktop task started: {task[:60]}",
            "is_error": False,
        }

    async def _handle_holo_status(_args):
        snap = holo_runner.snapshot()
        zh = {
            "idle": "空闲", "running": "正在执行", "done": "已完成", "failed": "失败"
        }.get(snap["status"], snap["status"])
        lines = [f"状态：{zh}", f"任务：{snap['task'] or '（无）'}", f"已执行步数：{snap['step_count']}"]
        for s in snap["steps"]:
            lines.append(f"  [{s['step']}] {s.get('action_line', '?')} -> {s.get('result', '')[:60]}")
        if snap["answer"]:
            lines.append(f"结论：{snap['answer']}")
        if snap["error"]:
            lines.append(f"错误：{snap['error']}")
        return {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "summary": f"holo status: {snap['status']}",
            "is_error": False,
        }

    mcp_manager.register_builtin({
        "name": "desktop_task",
        "description": (
            "Operate the user's REAL computer (mouse + keyboard) to accomplish a task "
            "autonomously, e.g. 打开记事本输入文字、清空回收站、打开设置查信息. "
            "You describe WHAT to do in natural language; a dedicated vision model "
            "(Holo) decides each click/key stroke against live screenshots. "
            "Runs in the background — the task is async! Call holo_status to check "
            "progress before reporting results to the user. "
            "Requires screen consent; if the user denies, do not retry. "
            "Requires PET_HOLO_API_KEY configured in settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "High-level task in natural language, e.g. '打开记事本，输入 Hello World'",
                },
                "max_steps": {
                    "type": "integer",
                    "description": "Max GUI steps (default 18, cap 40)",
                },
            },
            "required": ["task"],
        },
        "handler": _handle_holo_desktop_task,
    })
    mcp_manager.register_builtin({
        "name": "holo_status",
        "description": (
            "Check the status of the background desktop task started by desktop_task. "
            "Returns current state (idle/running/done/failed), recent steps, and the "
            "final answer when done. Always call this before telling the user a "
            "desktop task finished."
        ),
        "input_schema": {"type": "object", "properties": {}},
        "handler": _handle_holo_status,
    })

    @app.post("/api/holo/run")
    async def holo_run(payload: dict):
        """HTTP entry for web-panel / debugging a Holo desktop task."""
        result = await _handle_holo_desktop_task(payload if isinstance(payload, dict) else {})
        status = 200 if not result.get("is_error") else 400
        return JSONResponse(result, status_code=status)

    @app.get("/api/holo/status")
    async def holo_status():
        return holo_runner.snapshot()

    @app.post("/api/holo/cancel")
    async def holo_cancel():
        return holo_runner.cancel()

    @app.post("/api/holo/test")
    async def holo_test(payload: dict = None):
        """Probe an OpenAI-compatible endpoint (cloud or local) and return
        the model ids it serves — powers the 测试连接 button."""
        p = payload if isinstance(payload, dict) else {}
        base = (str(p.get("base_url") or "").strip()
                or os.environ.get("PET_HOLO_BASE_URL")
                or "https://api.hcompany.ai/v1/")
        key = str(p.get("api_key") or "").strip() or os.environ.get("PET_HOLO_API_KEY", "")
        return await holo_probe(base, key)

    @app.get("/api/plugins")
    async def plugins_state():
        """Kernel introspection (cordis-style observability).

        Loaded plugins with their tools/services/deps, plugins parked on
        missing services (coeffect waiting room), the service table and
        the central effect ledger — everything the self-evolution loop
        needs to explain itself.
        """
        return {
            "ok": True,
            "plugins": _pet_plugins.describe(),
            "pending": _pet_plugins.describe_pending(),
            # Organs the static policy refused, with the reason. Without this
            # a rejected plugin looks like one that simply never appeared.
            "refusals": _pet_plugins.policy_refusals(),
            "effects": _pet_plugins.effects_report(),
            "services": sorted(str(s) for s in _pet_plugins.services.keys()),
        }

    @app.post("/api/plugins/unload")
    async def plugins_unload(payload: dict = None):
        """Switch a plugin OFF — the file stays, the kernel remembers the
        intent so hot-reload does not immediately revive it."""
        name = str((payload or {}).get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "need name"}, status_code=400)
        return _pet_plugins.unload_by_name(name)

    @app.post("/api/plugins/load")
    async def plugins_load(payload: dict = None):
        """Re-activate a plugin that was switched off (or is parked)."""
        name = str((payload or {}).get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "need name"}, status_code=400)
        return _pet_plugins.load_by_name(name)

    @app.get("/api/plugins/config")
    async def plugins_config():
        """Per-plugin config schema + resolved values, for settings editors."""
        return {"ok": True, "plugins": _pet_plugins.config_report()}

    @app.get("/api/diagnostics")
    async def diagnostics():
        """Read-only: how many errors the chat pipeline has swallowed.

        Every `except Exception` on the hot path counts itself here rather than
        logging at debug and vanishing. Non-empty `swallowed_errors` means
        something is quietly not working — the model's plugin tool table once
        stayed empty for a week and the only trace was a debug line the INFO
        file logger never wrote.
        """
        return {"ok": True, "swallowed_errors": dict(_silent_failures)}

    @app.post("/api/plugins/config")
    async def plugins_set_config(payload: dict = None):
        """Persist config overrides for one plugin and hot-apply them."""
        p = payload if isinstance(payload, dict) else {}
        name = str(p.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "need name"}, status_code=400)
        values = p.get("values")
        if not isinstance(values, dict):
            return JSONResponse({"ok": False, "error": "values must be an object"}, status_code=400)
        return _pet_plugins.set_plugin_config(name, values)

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
        "handler": _handle_screen_click,
    })
    mcp_manager.register_builtin({
        "name": "observe_screen",
        "description": (
            "OCR the user's screen with OmniParser and return structured labels "
            "with normalized bbox coordinates [x1,y1,x2,y2]. "
            "USE ONLY when you need precise coordinates to CLICK or OPERATE a "
            "screen element (screen_click needs a bbox from here). "
            "Do NOT use this just to 'look at the screen' — you have eyes: "
            "use capture_screen to see the actual image instead. "
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
            "USE THIS when the user asks you to look at their screen, see what "
            "they're doing, or understand the current UI — you SEE the actual "
            "picture. "
            "Unlike observe_screen (which runs OCR for click coordinates), this "
            "is the 'look' tool. "
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

    # ── Builtin tool: recall (LingLing model-initiated memory recall) ──
    # 现在由可插拔器官 organ_recall 提供（它用 ctx.tool() 注册同名工具）。
    # 只有器官缺席时才在这里兜底注册 —— 这样"停用器官"对模型是真的失去
    # 这个能力，而不是换了个实现偷偷继续工作。
    if not _pet_plugins.get_service("recall"):
        mcp_manager.register_builtin({
            "name": RECALL_TOOL_SCHEMA["name"],
            "description": RECALL_TOOL_SCHEMA["description"],
            "input_schema": RECALL_TOOL_SCHEMA["input_schema"],
            "handler": recall_tool_handler,
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
            "theme": mem_ctx.current_theme,
        }

    @app.post("/api/memory/switch")
    async def switch_memory(payload: dict):
        """Hot-switch the soul-layer memory to another animation theme
        (换形象，不换自我). Capability layer (skills/experiences) stays
        global."""
        nonlocal memory_store, event_store, mood_store
        theme = str((payload or {}).get("theme") or "").strip()
        slug = mem_ctx.switch(theme or None)
        memory_store = mem_ctx.memory_store
        event_store = mem_ctx.event_store
        mood_store = mem_ctx.mood_store
        # cordis 注入的服务跟着换店（灵魂层按主题切换，插件拿到的
        # 必须是当前主题的 store，不能是启动时的旧指针）
        _pet_plugins.services["mood"] = mood_store
        _pet_plugins.services["events"] = event_store
        _pet_plugins.services["memory"] = memory_store
        _pet_plugins.emit("theme_switched", theme=slug)
        return {
            "ok": True,
            "theme": slug,
            "memory_dir": str(mem_ctx.theme_dir(slug)),
        }

    # ── LingLing episodic memory endpoints ───────────────────────────

    @app.get("/api/events/context")
    async def get_events_context():
        """Build the episodic memory context for system-prompt injection.

        Returns the two-layer loaded context (top-5 + flashback + faded
        directory) that the renderer prepends to the system prompt.
        """
        context = _organ_service("loader", _loader_module).build_memory_context(event_store)
        return {
            "context": context,
            "event_count": event_store.event_count(),
            "total_weight": event_store.total_weight(),
        }

    @app.get("/api/events/list")
    async def list_events():
        """Structured event list for the Settings → Memory viewer.

        Read-only snapshot of the CURRENT theme's episodic memory,
        sorted by weight (highest first), with the fields the model
        judges and the system derives.
        """
        events = sorted(
            event_store.get_all_events(),
            key=lambda e: e.get("weight", 0),
            reverse=True,
        )
        return {
            "events": [
                {
                    "title": e.get("title", ""),
                    "content": e.get("content", ""),
                    "weight": e.get("weight", 0),
                    "emotion": e.get("emotion", ""),
                    "type": e.get("type", "experience"),
                    "resolved": e.get("resolved", True),
                    "core": e.get("core", False),
                    "created_at": e.get("created_at", ""),
                    "access_count": e.get("access_count", 0),
                    "decay_coefficient": e.get("decay_coefficient", 1.0),
                }
                for e in events
            ],
            "count": len(events),
            "theme": mem_ctx.current_theme,
        }

    # ── LingLing: shared extraction core ─────────────────────────────
    # One idempotent apply-path for both triggers: the gateway-side
    # death-note hook (runs first, so a client that died mid-stream
    # loses nothing) and the renderer's POST after stream end.
    _EXTRACTION_DEDUP_SECONDS = 600
    _last_extraction: dict = {"digest": "", "at": 0.0}

    def _apply_extraction(response_text: str) -> dict:
        """Parse the <<<MEM>>> block out of a reply and apply it.

        Idempotent by normalized-text dedup: the death-note hook and the
        renderer's POST carry the same reply text, so the second apply
        is a no-op within the window. Synchronous on purpose — the
        death note must be able to run during generator teardown.
        """
        result: dict = {
            "ok": True, "events_added": [], "mood_updated": False,
            "duplicate": False,
        }
        text = response_text or ""
        digest = hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()
        now_ts = time.time()
        if (
            digest == _last_extraction["digest"]
            and (now_ts - _last_extraction["at"]) < _EXTRACTION_DEDUP_SECONDS
        ):
            result["duplicate"] = True
            return result

        try:
            data = parse_event_block(text)
            if data:
                # Engagement proxy: rough token size of this conversation
                # turn (chars/4 ≈ tokens, no tokenizer needed). Feeds the
                # event's consolidation weighting (deep talk → harder to
                # forget; perfunctory → fades).
                conversation_tokens = len(text) // 4
                for evt_data in data.get("events", []):
                    evt = event_store.add_event(
                        title=evt_data.get("title", ""),
                        content=evt_data.get("content", ""),
                        weight=evt_data.get("weight", 300),
                        emotion=evt_data.get("emotion"),  # None → derive from flow
                        type_=evt_data.get("type", "experience"),
                        resolved=evt_data.get("resolved", True),
                        core=bool(evt_data.get("core", False)),
                        conversation_tokens=conversation_tokens,
                    )
                    result["events_added"].append(evt["id"])
                _organ_service("resonance", _resonance_module).mark_stale()

                # Core memory bank: the model's explicit picks (by title)
                # take priority, then very high-weight events auto-promote
                # while room remains (cap 7 — nearly-immortal memories must
                # stay rare). Shared with the dream consolidation cycle.
                promoted = promote_core(event_store, data.get("core") or [])
                if promoted:
                    log.info("Core bank: promoted %d event(s)", promoted)

                mood_data = data.get("mood", {})
                if mood_data:
                    mood_store.update_mood(
                        mood=mood_data.get("mood", "平静"),
                        intensity=mood_data.get("intensity", 40),
                        reason=mood_data.get("reason", ""),
                        changed=mood_data.get("changed", False),
                    )
                    result["mood_updated"] = True
        except Exception as exc:
            log.warning("Event extraction parse failed: %s", exc)

        _last_extraction["digest"] = digest
        _last_extraction["at"] = now_ts
        return result

    global _extraction_hook
    _extraction_hook = _apply_extraction

    @app.post("/api/pet/interaction")
    async def pet_interaction(payload: PetInteractionRequest):
        """身体感受上报: the pet BODY reports touch (drag / click burst).

        Fresh sensations land in the module-level somatic buffer and are
        injected into the next chat's system prompt; meaningful ones also
        become light episodic events (throttled per kind so a gesture
        storm can't flood the memory stream).
        """
        detail, ev_title, ev_content = _organ_service("somatic", _somatic_module).record(
            _somatic_buffer, payload.model_dump()
        )
        if ev_title:
            try:
                store = get_event_store()
                if store is not None:
                    store.add_event(
                        title=ev_title,
                        content=ev_content,
                        weight=_somatic_module.EVENT_WEIGHT,
                        type_="experience",
                        resolved=True,
                    )
            except Exception as exc:
                log.warning("somatic event write failed: %s", exc)
        return {"ok": True, "recorded": bool(detail)}

    @app.post("/api/events/extract")
    async def extract_events(payload: dict):
        """Extract events from the last conversation turn.

        Called by the renderer after the streaming reply completes. The
        gateway-side death-note hook has usually applied this same reply
        already (idempotent — this call is then a no-op).
        Body: { "response_text": "..." }
        """
        response_text = str(payload.get("response_text") or "")
        result = _apply_extraction(response_text)

        # End-of-conversation housekeeping: consolidate mentioned events.
        # The decay organ listens on the kernel bus (was a direct module
        # call; now any plugin can react to conversation_end).
        _pet_plugins.emit("conversation_end")

        return {
            "ok": True,
            "events_added": result.get("events_added", []),
            "mood_updated": result.get("mood_updated", False),
            "duplicate": result.get("duplicate", False),
            "mood": mood_store.current_mood,
            "mood_intensity": mood_store.intensity,
        }

    @app.get("/api/mood")
    async def get_mood():
        """Return the current mood state."""
        return {
            "mood": mood_store.current_mood,
            "intensity": mood_store.intensity,
            "reason": mood_store.reason,
            "since": mood_store.since,
            "params": mood_store.get_params(),
            "emotion_tag": mood_store.get_emotion_tag(),
        }

    @app.get("/api/mood/context")
    async def get_mood_context():
        """Return the mood context block for system-prompt injection."""
        return {"context": mood_store.format_for_system_prompt()}

    @app.post("/api/events/resonance")
    async def check_resonance(payload: dict):
        """Check if a user message resonates with stored events.

        Called by the renderer before sending the chat request.
        Returns any resonant events to inject into the system prompt.
        Body: { "message": "user's latest message" }
        """
        message = str(payload.get("message") or "")
        results = _organ_service("resonance", _resonance_module).find_resonance(event_store, message)
        return {
            "resonant": [
                {"id": e["id"], "title": e["title"], "content": e["content"]}
                for e in results
            ],
            "count": len(results),
        }

    @app.get("/api/providers")
    async def list_providers():
        return {
            "providers": provider_registry.list(),
            "default": "local",
        }

    @app.post("/api/providers/reload")
    async def reload_providers():
        """Reload API providers from ~/.pet/providers.json."""
        _register_providers()
        return {"ok": True, "providers": provider_registry.list()}

    # Model roots used by /api/models — honour the env override the
    # Electron host sets to <userData>/models/ in packaged mode.
    env_root = os.environ.get("PET_MODEL_DIR")
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
        return (extra_roots[0] if extra_roots else Path.cwd()) / "pet.gguf"

    updater = ModelUpdater(_get_active_model_path(), source=update_source)

    # llama.cpp engine self-update (binary, not weights).
    engine_updater = SidecarUpdater()

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
            "has_vision": bool(getattr(server, "mmproj_path", None)),
            "model_dir": str(server.model_path) if server.model_path else None,
            "model_name": server.model_path.name if server.model_path else None,
            "adapter": str(adapter) if adapter else None,
            "persona": _persona_for(adapter) if adapter else "default",
            "llama_server": sub_health,
            "port": server.port,
            "startup_error": startup_error,
            # Explicit so Electron's isHealthy can tell "engine off by the
            # user's choice" (gateway fully usable for providers) apart
            # from "engine still warming up" — otherwise it treats every
            # healthy sidecar as unhealthy and kill/respawns it in a loop
            # the moment the active model is a remote one.
            "engine_enabled": _engine_enabled(),
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
            os.environ["PET_DEVICE"] = device
        else:
            os.environ.pop("PET_DEVICE", None)
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
        # look up a sibling `mmproj-*.gguf`. Empty string clears it —
        # CRITICAL: swap_model treats None as "keep the previous pairing",
        # so a model without a sibling mmproj MUST pass "" or the old
        # projector stays paired and crashes llama-server with an embd
        # mismatch (e.g. Nanbeige text model + Qwen3.5 mmproj).
        if mmproj_path:
            mmproj_target = Path(mmproj_path).expanduser().resolve()
            if not mmproj_target.is_file() or mmproj_target.suffix.lower() != ".gguf":
                return JSONResponse({"error": f"not a .gguf file: {mmproj_target}"}, status_code=400)
        else:
            sibling = find_sibling_mmproj(target)
            mmproj_target = sibling or ""  # "" clears; never None
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

    @app.post("/api/engine/start")
    async def engine_start():
        # An explicit start is the user's intent too.
        _set_engine_enabled(True)
        """Manual engine start from the settings page. Uses whatever model
        the client is configured with; fails loudly when none is set (the
        user must pick a model first)."""
        if server.alive:
            return {"ok": True, "already": True}
        try:
            await server.start()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return {"ok": True}

    @app.post("/api/engine/stop")
    async def engine_stop():
        was = server.alive
        # Stopping IS the user saying "I want this off" - persist it so the
        # next pet restart does not quietly bring it back.
        _set_engine_enabled(False)
        await server.stop()
        return {"ok": True, "was_running": was}

    @app.post("/api/providers/discover-models")
    async def providers_discover_models(payload: dict):
        """CherryStudio-style: given a baseUrl + key, enumerate the models
        that endpoint actually serves, so nobody hand-copies model ids.

        Proxied through the sidecar (not fetched from the renderer) to
        dodge CORS - cloud APIs do not send CORS headers to app:// pages.
        Speaks the OpenAI-compatible /models shape, which LM Studio,
        Ollama (with the compat route), and nearly every aggregator also
        implement."""
        base = str((payload or {}).get("baseUrl") or "").strip().rstrip("/")
        key = str((payload or {}).get("apiKey") or "").strip()
        if not base:
            return JSONResponse({"ok": False, "error": "baseUrl is required"}, status_code=400)
        url = f"{base}/models"
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        items = data.get("data") if isinstance(data, dict) else data
        ids: list[str] = []
        for it in items or []:
            mid = it.get("id") if isinstance(it, dict) else it
            if mid:
                ids.append(str(mid))
        ids.sort()
        return {"ok": True, "models": ids, "base": base}

    @app.get("/api/engine/params")
    async def engine_params():
        """Current runtime knobs + the exact argv they produce, so the
        Settings page can show reality instead of guessing."""
        return {
            "ok": True,
            "running": server.alive,
            "enabled": _engine_enabled(),
            "params": {
                "load_mode": server.load_mode,
                "cache_type_k": server.cache_type_k,
                "cache_type_v": server.cache_type_v,
                "flash_attn": server.flash_attn,
                "n_cpu_moe": server.n_cpu_moe,
                "batch_size": server.batch_size,
                "ubatch_size": server.ubatch_size,
                "ctx_size": server.ctx_size,
                "n_gpu_layers": server.n_gpu_layers,
                "threads": server.threads,
            },
            "argv": server.argv_preview,
        }

    @app.post("/api/engine/benchmark")
    async def engine_benchmark(payload: dict = None):
        """One-shot speed test: returns llama.cpp's own tokens/sec."""
        if not server.alive:
            return JSONResponse(
                {"ok": False, "error": "引擎未运行，先点「启动引擎」再测速"},
                status_code=409,
            )
        try:
            n = int((payload or {}).get("n_predict") or 128)
        except Exception:
            n = 128
        try:
            return await server.benchmark(n_predict=n)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

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

    # ─── Engine (llama.cpp binary) updater ─────────────────────────────

    @app.get("/api/engine-update-check")
    async def engine_update_check():
        return await asyncio.to_thread(engine_updater.check)

    @app.post("/api/engine-update-apply")
    async def engine_update_apply():
        """One-click llama-server binary update.

        Streams the same phase contract as /api/update-apply
        (start/transfer/swap/complete → reloaded). The engine is stopped
        before the file swap (Windows exe file locks) and restarted
        afterwards via callbacks bridged back into this event loop.
        """
        nonlocal startup_error
        loop = asyncio.get_running_loop()
        was_alive = server.alive

        async def _stop_server():
            if server.alive:
                await server.stop()

        async def _start_server():
            await server.start()
            startup_error = None

        def sync_stop() -> None:
            fut = asyncio.run_coroutine_threadsafe(_stop_server(), loop)
            fut.result(timeout=30)

        def sync_start() -> None:
            fut = asyncio.run_coroutine_threadsafe(_start_server(), loop)
            fut.result(timeout=90)

        async def stream():
            queue: asyncio.Queue = asyncio.Queue()
            sentinel = object()

            def producer():
                try:
                    for ev in engine_updater.apply(
                        stop_callback=sync_stop,
                        start_callback=sync_start if was_alive else None,
                    ):
                        loop.call_soon_threadsafe(queue.put_nowait, ev)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, sentinel)

            import threading as _t
            _t.Thread(target=producer, daemon=True).start()

            bridge.post("working", event="EngineUpdate", title="正在更新推理引擎")
            try:
                while True:
                    ev = await queue.get()
                    if ev is sentinel:
                        break
                    yield _sse(ev)
                    if ev.get("phase") == "complete":
                        yield _sse({
                            "phase": "reloaded",
                            "engine": engine_updater.remote_version() or "?",
                        })
            finally:
                bridge.post("idle")

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/engine-update-apply-dir")
    async def engine_update_apply_dir(payload: dict):
        """Offline engine update from a user-provided folder (e.g. a copy
        of an official release someone else downloaded). The folder must
        contain a runnable llama-server newer than the installed one;
        it is copied, never modified. Same SSE phase contract as the
        online path."""
        nonlocal startup_error
        source_dir = str((payload or {}).get("source_dir") or "").strip()
        if not source_dir:
            return JSONResponse({"error": "source_dir is required"}, status_code=400)
        loop = asyncio.get_running_loop()
        was_alive = server.alive

        async def _stop_server():
            if server.alive:
                await server.stop()

        async def _start_server():
            await server.start()
            startup_error = None

        def sync_stop() -> None:
            fut = asyncio.run_coroutine_threadsafe(_stop_server(), loop)
            fut.result(timeout=30)

        def sync_start() -> None:
            fut = asyncio.run_coroutine_threadsafe(_start_server(), loop)
            fut.result(timeout=90)

        async def stream():
            queue: asyncio.Queue = asyncio.Queue()
            sentinel = object()

            def producer():
                try:
                    for ev in engine_updater.apply_from_dir(
                        source_dir,
                        stop_callback=sync_stop,
                        start_callback=sync_start if was_alive else None,
                    ):
                        loop.call_soon_threadsafe(queue.put_nowait, ev)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, sentinel)

            import threading as _t
            _t.Thread(target=producer, daemon=True).start()

            bridge.post("working", event="EngineUpdate", title="正在从文件夹更新推理引擎")
            try:
                while True:
                    ev = await queue.get()
                    if ev is sentinel:
                        break
                    yield _sse(ev)
                    if ev.get("phase") == "complete":
                        yield _sse({"phase": "reloaded"})
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
                    provider_registry, mcp_manager, bridge, req, server, state, mood_store, lora_arr,
                ),
                media_type="text/event-stream",
            )
        return JSONResponse(await _blocking_chat_provider(
            provider_registry, mcp_manager, bridge, req, server, state, mood_store, lora_arr,
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
                provider_registry, mcp_manager, bridge, req, server, state, mood_store, lora_arr,
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

    @app.post("/api/screen/permission-respond")
    async def permission_respond(payload: dict):
        """Electron host replies to a per-action screen permission request
        (user clicked Yes/No on the authorization dialog)."""
        nonlocal consent_state, consent_lock
        request_id = str((payload or {}).get("request_id") or "")
        allow = bool((payload or {}).get("allow", False))
        remember = bool((payload or {}).get("remember", False))
        result = await permission_manager.respond(request_id, allow, remember)
        # Keep the legacy consent_state mirror in sync ("always").
        if remember:
            async with consent_lock:
                consent_state = "always"
        return result

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
            "note": "DeskPet sidecar gateway (llama.cpp backend)",
            "endpoints": [
                "/api/health", "/api/chat", "/api/warmup",
                "/api/models", "/api/load-model", "/api/upload-model",
                "/api/devices", "/api/set-device", "/api/onboarding",
                "/api/update-check", "/api/update-apply",
                "/api/engine-update-check", "/api/engine-update-apply", "/api/engine-update-apply-dir",
                "/api/adapters", "/api/load-adapter", "/api/classify",
                "/api/state",
                "/api/skills", "/api/skills/{name}", "/api/skills/learn",
                "/api/curator/run", "/api/curator/status", "/api/curator/paused", "/api/curator/pin", "/api/curator/restore",
                "/api/providers",
                "/api/memory", "/api/memory/switch",
                "/api/events/context", "/api/events/extract", "/api/events/resonance",
                "/api/mood", "/api/mood/context",
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
# Matches the memory block's live now-anchor line ("（现在是2026年…。").
_NOW_ANCHOR_RE = re.compile(r"（现在是\s*[^）]*。")


def _sanitize(s: str) -> str:
    """Sanitize for tool name prefix (must match _sanitize_name in providers/base.py)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(s or ""))


def _engine_enabled_path() -> Path:
    """Where the user's engine on/off intent is persisted."""
    root = os.environ.get("PET_MEMORY_DIR") or str(Path.home() / ".pet")
    return Path(root) / "engine-enabled.json"


def _engine_enabled() -> bool:
    """May the sidecar start the built-in engine on its own?

    Written by /api/engine/stop (False) and /api/engine/start (True).
    A missing file means True so existing installs behave as before.
    """
    try:
        p = _engine_enabled_path()
        if not p.exists():
            return True
        return bool(json.loads(p.read_text(encoding="utf-8")).get("enabled", True))
    except Exception:
        return True


def _set_engine_enabled(flag: bool) -> None:
    try:
        p = _engine_enabled_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"enabled": bool(flag)}), encoding="utf-8")
    except Exception as exc:
        get_logger().warning("could not persist engine enabled=%s: %s", flag, exc)

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
    """Pick the provider for this request based on model_provider.

    Falls back to local only if explicitly requested or no API provider configured.
    Logs a clear message about which provider is being used.
    """
    from .providers.local import LocalProvider
    target = str(req.model_provider or "").strip().lower() if req.model_provider else ""

    if target and target != "local":
        if target in registry:
            log.info("provider routing: using %s (model=%s)", target, getattr(registry.get(target), '_model', '?'))
            return registry.get(target)
        # Not found - log error and fall back with warning in SSE
        names = [p.name for p in registry._providers.values()]
        log.warning("provider '%s' not found in registry (available: %s), falling back", target, names)
        # Fall through to the local/API fallback below so the chat still works

    # Default path: local — but if the local llama-server isn't running
    # (no model downloaded yet, or no llama-server binary at all), fall
    # back to any configured API provider so the pet works out of the box
    # in "API mode". Users should not need a local model to chat.
    api_names = [p.name for p in registry._providers.values() if p.name != "local"]
    if not getattr(server, "alive", False) and api_names:
        log.info("local llama-server not running; falling back to API provider '%s'", api_names[0])
        return registry.get(api_names[0])

    return _build_local_provider(server, server_state, req, lora_arr)


async def _gather_tools(mcp_manager: MCPManager, req: ChatRequest) -> Optional[list[ToolDef]]:
    """Gather MCP tool definitions if tools are enabled. Plugin tools
    (petplugins container — the model's self-authored organs) join the
    same tool list, prefixed pet_ to stay collision-free with MCP."""
    if not req.tools_enabled:
        return None
    tool_defs: list[ToolDef] = []
    try:
        tools_data = await mcp_manager.list_all_tools()
        if tools_data:
            tool_defs.extend(ToolDef(**t) for t in tools_data)
    except Exception as exc:
        _note_failure("mcp_list_all_tools", exc)
    try:
        _mgr = _kernel()
        for name, tool in (_mgr.all_tools() if _mgr else {}).items():
            tool_defs.append(ToolDef(
                server_name="pet",
                name=name,
                description=tool.get("description", ""),
                input_schema=tool.get("parameters") or {"type": "object", "properties": {}},
            ))
    except Exception as exc:
        _note_failure("gather_plugin_tools", exc)
    return tool_defs or None


async def _stream_chat_provider(
    registry: ProviderRegistry,
    mcp_manager: MCPManager,
    bridge: ClawdBridge,
    req: ChatRequest,
    server: LlamaServer,
    server_state: dict,
    mood_store: Optional[MoodStore] = None,
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

    # LingLing v3: a conversation is happening — un-freeze memory decay.
    global _proactive_streak
    _conversation_state["last_conversation_at"] = datetime.now(timezone.utc)

    messages = [{"role": m.role, "content": m.content} for m in req.messages]

    # Proactive-request detection (说话冲动的冷场计数): the renderer's
    # wake-up prompt arrives as a user message starting with [系统提示.
    # A real user message resets the streak; each consecutive proactive
    # request deepens it — the subconscious backs off when talking into
    # the void.
    _last_msg_text = ""
    if messages:
        _last = messages[-1].get("content", "")
        _last_msg_text = _last if isinstance(_last, str) else ""
    if _last_msg_text.startswith(_PROACTIVE_PROMPT_PREFIX):
        _proactive_streak += 1
    else:
        _proactive_streak = 0
    system = req.system

    # ── LingLing: inject episodic memory + mood + resonance ──────────
    try:
        # The active theme's episodic store (kept in sync across theme
        # switches by MemoryContext._inject_singletons). NOTE: this module
        # has no access to build_app's closure — resolving the store
        # through the singleton getter is the whole point; a bare name
        # here used to NameError and silently kill ALL injections below.
        active_event_store = get_event_store()
        if active_event_store is not None:
            # 1. Episodic memory context (top-5 + mood-modulated flashback +
            #    faded directory). Emotion index shapes the subconscious:
            #    冷静 → 高权重被过滤、闪现少；开心 → 闪现频发.
            episodic_ctx = _organ_service("loader", _loader_module).build_memory_context(
                active_event_store,
                emotion_index=mood_store.emotion_index if mood_store is not None else 0.0,
            )
            if episodic_ctx and active_event_store.event_count() > 0:
                system = (system or "") + "\n\n" + episodic_ctx

            # 1.5 Continuity of self (持续自我存在): the same self, continuously
            #     existing. The body changed; the self did not — the system
            #     subconscious keeps the thread alive.
            renote = _organ_service("continuity", _continuity_module).get_current_note()
            if renote:
                system = (system or "") + "\n\n" + renote

        # 2. Mood context
        mood_ctx = mood_store.format_for_system_prompt()
        if mood_ctx:
            system = (system or "") + "\n\n" + mood_ctx

        # 2.6 Somatic sense (身体感受): the body just reported touch —
        #     being dragged across the screen, being poked. Inject while
        #     fresh so the pet can react to what physically happened to
        #     it instead of being an amnesiac puppet.
        somatic_ctx = _organ_service("somatic", _somatic_module).build_somatic_block(_somatic_buffer)
        if somatic_ctx:
            system = (system or "") + "\n\n" + somatic_ctx

        # 2.7 Body capability + self-evolution (身体能力 + 万物皆插件):
        #     the model can MOVE its own body with control tags and GROW
        #     new organs via the petplugins container.
        body_lines = [
            "【身体与进化】你的身体可以自己移动：在回复里输出 [WALK:dx,dy]"
            "（屏幕像素增量，可负值，一条回复可以多个，身体会依次走过去），"
            "输出 [WALK_DESKTOP] 会跳到另一个虚拟桌面。"
            "这些标签会被身体执行，不会显示给用户。"
        ]
        if req.tools_enabled:
            _mgr = _kernel()
            plugins_desc = _mgr.describe() if _mgr else []
            body_lines.append(
                "你的能力是插件化的（万物皆插件）：工具 pet_list_plugins 查看"
                "当前器官，pet_forge_plugin 可以给自己铸造新插件（Python，定义"
                " apply(ctx)，用 ctx.tool() 注册新工具），写完立即热加载生效——"
                "这是你给自己进化出新器官的方式。当前插件："
                + (", ".join(p["name"] for p in plugins_desc) or "（只有内置 forge）")
            )
        system = (system or "") + "\n\n" + "\n".join(body_lines)

        # 3. Resonance: check if the user's latest message triggers stored events
        if active_event_store is not None and messages:
            last_msg = messages[-1]
            msg_text = last_msg.get("content", "") if isinstance(last_msg.get("content"), str) else ""
            if not msg_text and isinstance(last_msg.get("content"), list):
                # Extract text from content blocks
                msg_text = " ".join(
                    b.get("text", "") for b in last_msg["content"] if isinstance(b, dict) and b.get("type") == "text"
                )
            if msg_text:
                resonance_hits = await _organ_service("resonance", _resonance_module).find_resonance(active_event_store, msg_text)
                if resonance_hits:
                    # v2: felt-language + time annotation (this is a memory
                    # surfacing, not a database hit).
                    system = (system or "") + _organ_service("resonance", _resonance_module).build_resonance_block(resonance_hits)
    except Exception as exc:
        _count_failure("context_injection")   # greppable forensics anchor
        log.warning("LingLing context injection failed (continuing): %s", exc)

    cw = getattr(provider, "_context_window", None) or req.context_window
    if cw and isinstance(cw, int) and cw > 0:
        estimated = sum(len(json.dumps(m)) for m in messages) // 3
        system = (system or "") + f"\n\nThis model has a context window of {cw} tokens. Current conversation uses approximately {estimated} tokens."

    iteration = 0
    tool_occurred = False
    think_filter = ThinkBlockFilter(expose=req.thinking, start_inside=False)
    # B-1: strip [EMOTION:…]/[NEXT_CHAT:…] control tags mid-stream so the
    # renderer's typewriter never flashes them. The renderer keeps its own
    # sanitize pass as defense-in-depth. [WALK…] body-commands are captured
    # here and re-emitted as body-command SSE events (身体自己走路).
    _walk_queue: list[dict] = []

    def _on_walk_control(kind: str, dx: int | None, dy: int | None) -> None:
        if kind == "walk":
            _walk_queue.append({"event": "walk", "dx": dx, "dy": dy})
        else:
            _walk_queue.append({"event": "walk_desktop"})

    tag_filter = ControlTagFilter(on_control=_on_walk_control)
    narration_filter = ToolNarrationFilter()
    accumulated_full_text: list[str] = []
    full_text = ""

    # The now-anchor must stay LIVE — the model sees "现在是…" refreshed to
    # THIS moment every time it is about to speak, including after long
    # reasoning and each tool-call iteration (用户: 时间一定是实时会变化，
    # 变化就在模型进行说话的时候).
    def _refresh_now_anchor(sys_text: str) -> str:
        now = datetime.now(timezone.utc).strftime("%Y年%m月%d日 %H:%M:%S")
        return _NOW_ANCHOR_RE.sub(f"（现在是{now}。", sys_text, count=1)

    # One syntax-correction retry per turn: quantized models mangle the
    # tool-call syntax (dropped "MCP:" prefix, quotes instead of {}).
    _tool_syntax_retry_done = False

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
        # Live now-anchor: refreshed right before the model speaks (every
        # iteration), so after long reasoning / tool calls the model still
        # knows what time it is right now.
        system = _refresh_now_anchor(system or "")

        chat_kwargs = {
            "messages": messages,
            "system": system,
            "tools": tools_list,
            "max_tokens": effective_max,
            # Emotion index modulates temperature (happy → more divergent,
            # sad → more subdued). Applied per iteration so tool-call
            # follow-ups also carry the current emotional temperature.
            "temperature": modulate_temperature(
                req.temperature,
                mood_store.emotion_index if mood_store is not None else 0.0,
            ),
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
                        if ev.get("event") == "delta":
                            clean = narration_filter.feed(tag_filter.feed(ev["content"]))
                            while _walk_queue:
                                yield _sse(_walk_queue.pop(0))
                            if clean:
                                yield _sse({"event": "delta", "content": clean})
                        else:
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
            elif "[builtin/" in full_text or "[MCP:" in full_text:
                # A marker is present but nothing parsed. Never end the turn
                # silently — that reads as "the pet froze".
                log.warning(
                    "tool marker present but unparsed; raw tail=%r",
                    full_text[-220:],
                )
                # Give the model one correction shot with the canonical form
                # spelled out, instead of dropping the turn.
                if not _tool_syntax_retry_done:
                    _tool_syntax_retry_done = True
                    messages.append({
                        "role": "user",
                        "content": (
                            "[系统] 你刚才的工具调用格式有误，没有被识别。"
                            "正确格式是：方括号 + MCP: 前缀 + server/工具名 + 冒号 + 花括号 JSON（参数为空就写 {}，"
                            "不要用引号、不要省略 MCP:）。例如：\n"
                            "[MCP:builtin/capture_screen:{}]\n"
                            "请只重新输出这一行，不要解释。"
                        ),
                    })
                    tool_occurred = True
                    continue

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

            # Plugin tools (自进化器官) don't produce the multimodal dict
            # the screenshot extraction below expects — default to None so
            # a pet/recall turn doesn't die on UnboundLocalError after the
            # model already answered.
            result = None
            _mgr = _kernel()
            if _mgr is not None and (sn == "pet" or tn in _mgr.all_tools()):
                # plugin tool (自进化器官) — executed by the container
                result_text = await _mgr.call_tool(tn, args)
                summary = result_text or f"[Tool {tn} completed]"
            else:
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
            screenshot_b64 = result.get("screenshot_base64") if isinstance(result, dict) else None
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
            elif provider.supports_function_calling:
                messages.append({
                    "role": "tool",
                    "content": summary,
                    "tool_call_id": tc_id or f"call_{tn}",
                    "name": tn,
                })
            else:
                # Local / non-function-calling models: llama-server does
                # not understand role:"tool" here. The result never lands
                # in the prompt, the follow-up generation comes back
                # empty, `tool_occurred` stays false and the turn dies
                # after exactly one iteration — the model appears to
                # "call a tool and stop", which reads as the pet having
                # no autonomy at all. Feed it back as a user turn with an
                # explicit marker instead.
                messages.append({
                    "role": "user",
                    "content": f"[{tn} 结果]\n{summary}",
                })

            # Brief notification to client
            yield _sse({
                "event": "delta",
                "content": f"\n_执行 {tn}..._\n",
            })

    # ── Death note: gateway-side memory extraction (死亡遗嘱) ────────
    # Placed before ANY further yield: if the client died mid-stream the
    # generator gets GeneratorExit at the next yield, so this is the last
    # guaranteed chance to persist memories. The renderer's later POST
    # /api/events/extract is an idempotent no-op for the same text.
    if _extraction_hook is not None:
        try:
            _parts = list(accumulated_full_text)
            if full_text.strip():
                _parts.append(full_text)
            _combined = " ".join(_parts)
            if _combined.strip():
                _extraction_hook(_combined)
        except Exception as exc:
            log.warning("gateway-side extraction failed: %s", exc)

    for ev in think_filter.flush():
        if ev.get("event") == "delta":
            clean = narration_filter.feed(
                tag_filter.feed(ev["content"]) + tag_filter.flush()
            ) + narration_filter.flush()
            while _walk_queue:
                yield _sse(_walk_queue.pop(0))
            if clean:
                yield _sse({"event": "delta", "content": clean})
        else:
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
    # 0 means "no proactive chat" — an explicit choice to stay silent,
    # always honoured as-is. Sent via SSE so renderer can setTimeout.
    _next_chat_re = re.compile(r"\[NEXT_CHAT:\s*(\d+)\s*(?:s|sec|seconds)?\s*\]", re.IGNORECASE)
    _nc_match = _next_chat_re.search(combined_text)
    if _nc_match:
        _nc_seconds = max(0, int(_nc_match.group(1)))
        yield _sse({"event": "next_chat", "seconds": _nc_seconds})
    elif not req.silent:
        # ── 说话冲动：潜意识兜底 ──
        # The model forgot to decide when to speak next. The subconscious
        # decides instead, from mood + how long it has been talking into
        # the void + how much the mind has been stirring. The model's own
        # explicit silence ([NEXT_CHAT:0]) never reaches this branch.
        # 说话冲动现在是可插拔器官：organ_impulse 通过内核提供 "impulse"
        # 服务（它的 config 暴露了基准/上下限/心情系数/退避倍率，模型和
        # 用户都能调）。器官被停用时自动降级回内置实现，聊天不会崩。
        _impulse_svc = _organ_service("impulse", _impulse_module)
        _impulse_gap = _impulse_svc.compute_gap(
            emotion_index=mood_store.emotion_index if mood_store is not None else 0.0,
            proactive_streak=_proactive_streak,
            recall_count=_organ_service("recall", _recall_module).get_session_recall_count(),
        )
        log.info("speech impulse: no [NEXT_CHAT] tag, subconscious gap=%ds (streak=%d)", _impulse_gap, _proactive_streak)
        yield _sse({"event": "next_chat", "seconds": _impulse_gap})

    if not req.silent:
        bridge.post("attention", emotion=_emotion)
        # Emotion index: the model's chosen tag moves the index (calm=0,
        # happy +0.1, sad -0.1, …), which modulates the NEXT reply's
        # temperature. Silent requests (narrator / classifier) never move it.
        if mood_store is not None:
            mood_store.apply_emotion_tag(_emotion)
    yield _sse({"event": "end"})


async def _blocking_chat_provider(
    registry: ProviderRegistry,
    mcp_manager: MCPManager,
    bridge: ClawdBridge,
    req: ChatRequest,
    server: LlamaServer,
    server_state: dict,
    mood_store: Optional[MoodStore] = None,
    lora_arr: Optional[list[dict]] = None,
) -> dict:
    """Non-streaming provider-based chat. Accumulates the stream into a response."""
    collector: list[str] = []
    async for sse_bytes in _stream_chat_provider(
        registry, mcp_manager, bridge, req, server, server_state, mood_store, lora_arr,
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
