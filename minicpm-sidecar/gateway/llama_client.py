"""llama.cpp `llama-server` subprocess manager + OpenAI streaming client.

Owns the lifecycle of a single `llama-server` child:
  - resolve the binary (next to ourselves in packaged mode, or from
    <repo-root>/llama.cpp/build/bin in dev)
  - spawn it with the right `--model / --port / --ctx-size` flags
  - poll `GET /health` until ready
  - proxy `POST /v1/chat/completions` streams back to the caller
  - hot-swap models by killing and respawning with a new `--model`

The gateway HTTP layer (server.py) calls only the methods exposed here;
nothing else in the codebase should touch llama-server directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx

from .lifecycle import (
    cleanup_stale_llama_server,
    clear_pid_file,
    default_pid_file_path,
    pdeathsig_preexec,
    write_pid_file,
)
from .log_setup import get_logger


def _find_free_port(start: int = 18766, end: int = 18800) -> int:
    """Pick an available localhost port in a small range so logs / firewall
    rules stay predictable. Falls back to a random ephemeral port if every
    candidate is taken."""
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _normalise_device(raw: Optional[str] = None) -> str:
    sys_name = platform.system()
    machine = platform.machine().lower()
    device = (raw if raw is not None else os.environ.get("MINICPM_DEVICE", "auto")).strip().lower()
    if device == "mps":
        device = "metal"
    if sys_name == "Darwin" and device == "vulkan":
        return "metal" if machine in ("arm64", "aarch64") else "cpu"
    if device in ("", "auto"):
        if sys_name == "Windows":
            return "cpu"
        if sys_name == "Darwin" and machine in ("arm64", "aarch64"):
            return "metal"
        return "auto"
    return device


def _candidate_binary_paths(device: Optional[str] = None) -> list[Path]:
    """Where to look for llama-server, in priority order.

    1. $MINICPM_LLAMA_SERVER  (explicit override, dev convenience)
    2. Next to ourselves    — packaged sidecar lives in
       <resources>/sidecar-bin/  alongside llama-server[.exe]
    3. minicpm-sidecar/bin/<os>-<arch>/  — official llama.cpp release output
    4. <repo-root>/llama.cpp/build/bin/  — optional local CMake build fallback
    """
    sys_name = platform.system()
    exe = "llama-server.exe" if sys_name == "Windows" else "llama-server"
    selected = _normalise_device(device)
    windows_backend_dir = (
        "vulkan" if sys_name == "Windows" and selected == "vulkan"
        else "cuda" if sys_name == "Windows" and selected == "cuda"
        else None
    )
    override = os.environ.get("MINICPM_LLAMA_SERVER")
    out: list[Path] = []
    if override and not windows_backend_dir:
        out.append(Path(override).expanduser())

    if getattr(sys, "frozen", False):
        # PyInstaller: sys.executable is the gateway binary; its parent is
        # the install dir where llama-server sits too.
        base = Path(sys.executable).resolve().parent
        if windows_backend_dir:
            out.append(base / "backends" / windows_backend_dir / exe)
        else:
            out.append(base / exe)
    else:
        # Dev: file → gateway/ → minicpm-sidecar/ → repo-root/
        pkg_root = Path(__file__).resolve().parent.parent
        repo_root = pkg_root.parent
        triple = _platform_triple()
        if windows_backend_dir:
            out.append(pkg_root / "bin" / triple / "backends" / windows_backend_dir / exe)
            out.append(repo_root / "llama.cpp" / f"build-{triple}-{windows_backend_dir}" / "bin" / exe)
            out.append(repo_root / "llama.cpp" / f"build-{triple}-{windows_backend_dir}" / exe)
        else:
            out.append(pkg_root / "bin" / triple / exe)
            out.append(repo_root / "llama.cpp" / "build" / "bin" / exe)
            out.append(repo_root / "llama.cpp" / "build" / exe)

    # As a last resort, look it up on PATH so a `brew install llama.cpp`
    # checkout works in dev. Do not PATH-fallback explicit Windows backend
    # choices, or "Vulkan" can silently launch the default CPU binary.
    if not windows_backend_dir:
        which = shutil.which(exe)
        if which:
            out.append(Path(which))

    seen: set[Path] = set()
    unique: list[Path] = []
    for p in out:
        rp = p.resolve() if p.exists() else p
        if rp in seen:
            continue
        seen.add(rp)
        unique.append(p)
    return unique


def _platform_triple() -> str:
    """Match electron-builder's `${os}-${arch}` so packaged binaries land
    where extraResources expects them."""
    sys_name = platform.system()
    machine = platform.machine().lower()
    if sys_name == "Darwin":
        return "mac-arm64" if machine in ("arm64", "aarch64") else "mac-x64"
    if sys_name == "Linux":
        return "linux-arm64" if machine in ("aarch64", "arm64") else "linux-x64"
    if sys_name == "Windows":
        return "win-arm64" if machine in ("arm64", "aarch64") else "win-x64"
    return f"{sys_name.lower()}-{machine}"


def detect_backend() -> dict:
    """Best-effort report of which acceleration backend the llama-server
    binary likely uses. Pure heuristic: we just look at the OS/arch and
    whether common runtime libs are present. The actual backend gets
    confirmed by parsing llama-server's --version output once it starts."""
    sys_name = platform.system()
    machine = platform.machine().lower()
    current = _normalise_device()
    backends: list[str] = []
    experimental: list[str] = []
    reasons: dict[str, str] = {}
    if sys_name == "Windows":
        backends.append("cpu")
        reasons["cpu"] = "Stable CPU inference for Windows"
        vulkan_available = any(p.is_file() for p in _candidate_binary_paths("vulkan"))
        if vulkan_available:
            backends.append("vulkan")
            experimental.append("vulkan")
            reasons["vulkan"] = "Experimental Vulkan GPU backend"
        return {
            "available": backends,
            "recommended": "cpu",
            "current": current if current in backends else "cpu",
            "experimental": experimental,
            "reasons": reasons,
        }
    if sys_name == "Darwin" and machine in ("arm64", "aarch64"):
        backends.append("metal")
        reasons["metal"] = "Apple Silicon GPU (Metal)"
    # CUDA detection: presence of the nvidia-smi binary is a reasonable
    # proxy without dragging in the cuda-python wheel.
    if shutil.which("nvidia-smi"):
        backends.append("cuda")
        reasons["cuda"] = "NVIDIA GPU 检测到（nvidia-smi）"
    backends.append("cpu")
    reasons["cpu"] = "纯 CPU 推理，速度较慢但任何机器都能跑"
    return {
        "available": backends,
        "recommended": backends[0],
        "current": current if current in backends else backends[0],
        "experimental": experimental,
        "reasons": reasons,
    }


# Names we recognise as multimodal projector files when scanning a model
# directory. llama.cpp calls them `mmproj-*` (the canonical flag); Ophcrack /
# coqui and a few older projects use other prefixes. Match is case- and
# separator-insensitive via the suffix check below.
_MMPROJ_BASENAMES = (
    "mmproj",
)


# Model-family detection: we extract the leading family prefix from the
# model file name (before the quantisation tag) so we can require the
# sibling mmproj to belong to the same family. Without this check, two
# models sharing a directory and a quant tag (e.g. `MiniCPM5-1B-Q4` and
# `Llama-3.2-Vision-Q4`) would pair by quant tag alone, silently
# mismatching the vision projector head dimension and segfaulting
# llama-server on the first vision request.
def _model_family(stem: str) -> str:
    s = stem.lower()
    # Drop trailing quant tag and version suffixes first.
    for sep in ("-", "_"):
        if sep in s:
            tail = s.rsplit(sep, 1)[-1]
            if tail and tail[:1].isalpha():
                s = s.rsplit(sep, 1)[0]
                break
    # Match the family by the longest known prefix; fall back to the
    # whole stripped stem so unrelated families never collide.
    for family in ("minicpm", "minicpm-o", "llava", "qwen-vl", "qwen", "llama", "phi", "gemma"):
        if s.startswith(family):
            return family
    return s


def find_sibling_mmproj(model_path: Path) -> Optional[Path]:
    """If `model_path` lives next to a `mmproj-*.gguf` (a CLIP-style vision
    projector), return its resolved path. Used to automatically pair the
    active text checkpoint with its complementary vision encoder so
    llama-server can answer multimodal requests.

    Resolution rules:
      - Same directory as the model file, scan for *.gguf whose basename
        starts with any of the known projector prefixes
        (`mmproj-...-{f16,bf16,...}.gguf`, case-insensitive).
      - The projector's model family (minicpm, llava, qwen, llama, ...)
        must match the text model's family. Quant tag is only used as a
        tiebreaker so two `mmproj-*-q4.gguf` from different families
        never collide.
    Returns None when no projector file is present (whether because no
    mmproj lives there, or because none matches the model family).
    """
    if model_path is None:
        return None
    model_path = Path(model_path).expanduser().resolve()
    if not model_path.is_file():
        return None
    parent = model_path.parent
    model_stem = model_path.stem.lower()
    family = _model_family(model_stem)
    # Pull the quantisation tag (after the last hyphen) from the model
    # stem so we can prefer a matching mmproj when several exist. E.g.
    # MiniCPM5-1B-F16.gguf → "f16".
    quant = None
    for sep in ("-", "_"):
        if sep in model_stem:
            tail = model_stem.rsplit(sep, 1)[-1]
            if tail and tail[:1].isalpha():
                quant = tail
                break
    try:
        entries = list(parent.iterdir())
    except OSError:
        return None
    candidates: list[Path] = []
    all_mmproj: list[Path] = []  # all mmproj files, regardless of family
    for entry in entries:
        if not entry.is_file():
            continue
        name = entry.name.lower()
        if not name.endswith(".gguf"):
            continue
        # Match only `mmproj*.gguf` (any quantisation in between).
        if not name.startswith("mmproj"):
            continue
        all_mmproj.append(entry)
        # Prefer same-family projectors, but DON'T reject generic
        # mmproj-F32.gguf that doesn't mention the family name.
        # The family check is only a tiebreaker when multiple
        # mmproj files from different families coexist.
        if family and family not in entry.stem.lower():
            continue
        candidates.append(entry)
    # If no family-matched candidates but there IS a single generic
    # mmproj file, use it — the user put it there for this model.
    if not candidates and len(all_mmproj) == 1:
        candidates = all_mmproj
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Prefer a projector whose stem mentions the model's quant tag.
    if quant:
        scored = sorted(
            candidates,
            key=lambda p: (quant not in p.stem.lower(), p.name),
        )
        return scored[0]
    return candidates[0]


class LlamaServer:
    """Owns one llama-server subprocess + an httpx client that talks to it."""

    def __init__(
        self,
        *,
        model_path: Optional[Path],
        ctx_size: int = 4096,
        n_gpu_layers: int = -1,
        threads: Optional[int] = None,
        extra_args: Optional[list[str]] = None,
        adapters: Optional[list[Path]] = None,
        mmproj_path: Optional[Path] = None,
    ) -> None:
        self.model_path: Optional[Path] = Path(model_path).expanduser().resolve() if model_path else None
        # Optional CLIP-style vision projector. When the active model is a
        # multimodal checkpoint (Vision-Instruct / Omni…), llama-server
        # needs `--mmproj` to decode attached images. We accept either an
        # explicit path or auto-detection (handled by the caller) and pass
        # it through to the spawned argv.
        self.mmproj_path: Optional[Path] = (
            Path(mmproj_path).expanduser().resolve() if mmproj_path else None
        )
        self.ctx_size = int(ctx_size)
        self.n_gpu_layers = int(n_gpu_layers)
        self.device = _normalise_device()
        self.threads = threads
        self.extra_args: list[str] = list(extra_args or [])
        # Ordered list of GGUF LoRA paths pre-loaded into llama-server via
        # `--lora`. Index in this list matches the integer `id` llama-server
        # assigns at startup and that we later use in per-request `lora`
        # arrays. See `_refresh_adapter_index` for the verification step.
        self.adapter_paths: list[Path] = [
            Path(p).expanduser().resolve() for p in (adapters or [])
        ]
        self._adapter_index: dict[Path, int] = {}
        self.host = "127.0.0.1"
        self.port: int = 0
        self._proc: Optional[subprocess.Popen] = None
        self._binary: Optional[Path] = None
        self._client: Optional[httpx.AsyncClient] = None
        # Single-flight lock around start/stop so /api/load-model can't race
        # with a concurrent restart.
        self._swap_lock = threading.Lock()
        self.last_stderr: list[str] = []
        # Persisted pid of the llama-server child. Lets the *next* sidecar
        # boot reap an orphan that this process forgot to stop (e.g.
        # because we were `kill -9`'d before our FastAPI lifespan ran).
        self._pid_file: Path = default_pid_file_path()

    # ── lifecycle ───────────────────────────────────────────────────────

    def _resolve_binary(self) -> Path:
        for cand in _candidate_binary_paths(self.device):
            try:
                if cand.is_file():
                    return cand
            except Exception:
                continue
        searched = "\n  ".join(str(p) for p in _candidate_binary_paths(self.device))
        raise FileNotFoundError(
            "找不到 llama-server。请先下载官方 llama.cpp release：cd minicpm-sidecar && ./scripts/fetch-llama-release.sh。\n"
            f"  已检查的路径:\n  {searched}"
        )

    def _build_argv(self) -> list[str]:
        if not self.model_path:
            raise RuntimeError("model_path is empty; refusing to start llama-server")
        argv: list[str] = [
            str(self._binary),
            "--model", str(self.model_path),
            "--host", self.host,
            "--port", str(self.port),
            "--ctx-size", str(self.ctx_size),
            "--jinja",        # use the GGUF-embedded chat template
            "--no-webui",     # we drive everything via API
        ]
        if self.device != "cpu" and self.n_gpu_layers != 0:
            argv += ["--gpu-layers", str(self.n_gpu_layers)]
        if self.threads:
            argv += ["--threads", str(self.threads)]
        # Multimodal: if a CLIP/mmproj projector was paired with the model,
        # pass it through. llama-server only loads it when --mmproj is set
        # alongside --model, so the two flags must land in the same argv.
        if self.mmproj_path and self.mmproj_path.is_file():
            argv += ["--mmproj", str(self.mmproj_path)]
        # Pre-register every discovered GGUF LoRA. Even when no adapter is
        # currently "active" we keep them loaded so /api/load-adapter can
        # toggle them without restarting llama-server. The actual scaling
        # happens per-request via the OpenAI body's `lora` array, so the
        # global state here is harmless either way.
        for adapter in self.adapter_paths:
            argv += ["--lora", str(adapter)]
        if self.adapter_paths:
            argv += ["--lora-init-without-apply"]
        argv += self.extra_args
        return argv

    async def start(self) -> None:
        log = get_logger()
        with self._swap_lock:
            if self._proc and self._proc.poll() is None:
                log.debug("llama-server already running on :%d", self.port)
                return
            # Before claiming a port, sweep any orphan llama-server from a
            # previous sidecar crash. Without this, an orphan keeps
            # holding :18766 and we'd silently land on :18767 every restart
            # while the dead-but-alive process bleeds memory.
            cleanup_stale_llama_server(self._pid_file)
            self.device = _normalise_device()
            self._binary = self._resolve_binary()
            self.port = _find_free_port()
            argv = self._build_argv()
            log.info("spawn llama-server: %s", " ".join(argv))
            env = os.environ.copy()
            # Quiet llama.cpp's default progress bars in the parent log;
            # we surface progress to Electron via our own /api/update-apply
            # stream and llama-server's own JSON logs.
            env.setdefault("LLAMA_ARG_NO_DISPLAY_PROMPT", "1")
            popen_kwargs: dict = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "bufsize": 1,
                "env": env,
            }
            # Linux: ask the kernel to SIGTERM llama-server the moment we
            # die, even if we die via SIGKILL and never run any cleanup
            # ourselves. macOS / Windows have no preexec_fn — they rely
            # on the PID-file-on-next-boot path (cleanup_stale_llama_server
            # above) for the same scenario.
            preexec = pdeathsig_preexec()
            if preexec is not None:
                popen_kwargs["preexec_fn"] = preexec
            try:
                self._proc = subprocess.Popen(argv, **popen_kwargs)
            except Exception as exc:
                log.exception("llama-server spawn failed: %s", exc)
                raise
            # Record pid AFTER spawn succeeds so a failed exec doesn't
            # leave a bogus pid file pointing at our own process.
            write_pid_file(self._pid_file, self._proc.pid)
            self._spawn_tailer(self._proc)
            self._client = httpx.AsyncClient(
                base_url=f"http://{self.host}:{self.port}",
                timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
            )
        # Health poll outside the swap lock so /api/chat issued by Electron
        # before this returns doesn't deadlock.
        await self._await_ready(timeout=90.0)
        # Pin the adapter id ↔ path mapping. llama-server assigns ids by
        # the order of --lora flags, but we cross-check against the live
        # /lora-adapters response so a future llama.cpp behaviour change
        # can't quietly desynchronise our per-request `lora` arrays.
        await self._refresh_adapter_index()

    def _spawn_tailer(self, proc: subprocess.Popen) -> None:
        log = get_logger()

        def reader(stream, kind: str) -> None:
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    line = line.rstrip()
                    if kind == "stderr":
                        self.last_stderr.append(line)
                        if len(self.last_stderr) > 80:
                            self.last_stderr.pop(0)
                        log.warning("[llama-server] %s", line)
                    else:
                        log.info("[llama-server] %s", line)
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        threading.Thread(target=reader, args=(proc.stdout, "stdout"), daemon=True).start()
        threading.Thread(target=reader, args=(proc.stderr, "stderr"), daemon=True).start()

    async def _await_ready(self, *, timeout: float) -> None:
        log = get_logger()
        deadline = time.monotonic() + timeout
        last_err: Optional[Exception] = None
        async with httpx.AsyncClient(timeout=2.0) as probe:
            while time.monotonic() < deadline:
                if self._proc and self._proc.poll() is not None:
                    tail = "\n".join(self.last_stderr[-30:]) or "(no stderr)"
                    raise RuntimeError(
                        f"llama-server exited early code={self._proc.returncode}\n----- stderr tail -----\n{tail}"
                    )
                try:
                    r = await probe.get(f"http://{self.host}:{self.port}/health")
                    if r.status_code == 200:
                        log.info("llama-server ready on :%d", self.port)
                        return
                except Exception as exc:
                    last_err = exc
                await asyncio.sleep(0.4)
        raise TimeoutError(
            f"llama-server did not become ready in {timeout:.0f}s "
            f"(last probe error: {last_err})"
        )

    async def stop(self, *, timeout: float = 5.0) -> None:
        log = get_logger()
        with self._swap_lock:
            client = self._client
            self._client = None
            proc = self._proc
            self._proc = None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
        if proc and proc.poll() is None:
            try:
                if platform.system() == "Windows":
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGTERM)
            except Exception:
                pass
            for _ in range(int(timeout * 10)):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if proc.poll() is None:
                log.warning("llama-server didn't exit, SIGKILL")
                try:
                    proc.kill()
                except Exception:
                    pass
        # llama-server is gone (or we tried our best); drop the pid
        # file so the *next* sidecar boot doesn't waste cycles probing
        # a now-dead pid.
        clear_pid_file(self._pid_file)

    async def swap_model(self, model_path: Path, mmproj_path: Optional[Path] = None) -> None:
        """Restart llama-server with a different `--model` (and optional mmproj).

        mmproj_path=None leaves the previous projector paired; pass an
        explicit path to switch / clear it.
        """
        await self.stop()
        self.model_path = Path(model_path).expanduser().resolve()
        if mmproj_path is not None:
            self.mmproj_path = Path(mmproj_path).expanduser().resolve() if mmproj_path else None
        await self.start()

    async def reload_adapters(self, paths: list[Path]) -> None:
        """Restart llama-server with a different `--lora` set.

        Used when the user drops a new GGUF LoRA into `<userData>/adapters/`
        and clicks "refresh" in Settings, or when /api/load-adapter is
        asked to activate a path that wasn't `--lora`-loaded at spawn time.

        The base model is unchanged. Cost is one llama-server restart
        (~2-4s on M-series Metal), same order of magnitude as
        /api/load-model.
        """
        new_paths = [Path(p).expanduser().resolve() for p in paths]
        # No-op fast path: same set in same order, nothing to do.
        if new_paths == self.adapter_paths:
            return
        await self.stop()
        self.adapter_paths = new_paths
        await self.start()

    async def _refresh_adapter_index(self) -> None:
        """Populate `_adapter_index` from the live llama-server state.

        llama-server exposes `GET /lora-adapters` returning a list of
        `{id, path, scale, ...}`. We map our absolute Path objects against
        whatever path strings llama-server reports so future schema tweaks
        (e.g. resolved-vs-symlink paths) don't silently break per-request
        routing."""
        log = get_logger()
        self._adapter_index = {}
        if not self.adapter_paths or not self._client:
            return
        try:
            resp = await self._client.get("/lora-adapters")
            resp.raise_for_status()
            entries = resp.json() or []
        except Exception as exc:
            log.warning("could not read /lora-adapters from llama-server: %s", exc)
            # Fall back to positional matching: --lora ordering equals id
            # ordering in every llama.cpp version we've tested.
            for idx, path in enumerate(self.adapter_paths):
                self._adapter_index[path] = idx
            return

        # Resolve both sides before comparing so ../foo vs realpath/foo
        # don't slip through.
        by_resolved: dict[Path, int] = {}
        for entry in entries:
            try:
                p = Path(entry["path"]).expanduser().resolve()
                by_resolved[p] = int(entry["id"])
            except Exception:
                continue
        for path in self.adapter_paths:
            resolved = path.expanduser().resolve()
            if resolved in by_resolved:
                self._adapter_index[path] = by_resolved[resolved]
        if len(self._adapter_index) != len(self.adapter_paths):
            log.warning(
                "adapter index incomplete: requested %d, resolved %d",
                len(self.adapter_paths),
                len(self._adapter_index),
            )

    def adapter_id_for(self, path: Path) -> Optional[int]:
        """Return the llama-server LoRA id for a given local path, or None
        if the adapter wasn't pre-loaded."""
        try:
            resolved = Path(path).expanduser().resolve()
        except Exception:
            return None
        return self._adapter_index.get(resolved)

    # ── runtime info ────────────────────────────────────────────────────

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def health(self) -> Optional[dict]:
        if not self._client:
            return None
        try:
            r = await self._client.get("/health")
            if r.status_code == 200:
                return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"ok": True}
        except Exception:
            return None
        return None

    # ── chat streaming ──────────────────────────────────────────────────

    async def stream_chat(
        self,
        *,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        top_p: float,
        top_k: int,
        repetition_penalty: float,
        stop: Optional[list[str]] = None,
        enable_thinking: bool = True,
        lora: Optional[list[dict]] = None,
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield ``(kind, text)`` tuples from llama-server's OpenAI stream.

        `kind` is one of:
          - ``"content"`` — assistant reply text
          - ``"reasoning"`` — pre-split <think> block (llama.cpp emits this
            into a dedicated ``delta.reasoning_content`` field when the
            chat template includes a thinking section and we boot with
            ``--jinja``)

        We surface both kinds so the gateway can route them into the
        right SSE event (``event: think`` vs ``event: delta``). When the
        upstream stream interleaves them (rare, but technically allowed)
        the order is preserved.
        """
        if not self._client:
            raise RuntimeError("llama-server client not initialised; did you await start()?")

        body = {
            "model": "minicpm",
            "messages": messages,
            "stream": True,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "top_p": float(top_p),
            # llama-server uses OpenAI naming for top_k / repeat_penalty
            # via its extended schema:
            "top_k": int(top_k) if top_k and top_k > 0 else 0,
            "repeat_penalty": float(repetition_penalty),
            # Forwarded to the GGUF-embedded Jinja template. MiniCPM5's
            # template treats this as "skip the <think>\n prefill",
            # which truly disables reasoning rather than just hiding it.
            "chat_template_kwargs": {"enable_thinking": bool(enable_thinking)},
        }
        if stop:
            body["stop"] = stop
        # Per-request LoRA override (llama.cpp PR #10994). `lora=[]`
        # explicitly disables every pre-loaded adapter for THIS request
        # only — used by narration to bypass the persona without touching
        # global state. `lora=None` means "fall through to llama-server's
        # current global scales", which lets a base-only chat happen even
        # when adapters are pre-loaded.
        if lora is not None:
            body["lora"] = lora

        async with self._client.stream("POST", "/v1/chat/completions", json=body) as resp:
            if resp.status_code != 200:
                tail = (await resp.aread()).decode("utf-8", "ignore")[:500]
                raise RuntimeError(f"llama-server /v1/chat/completions HTTP {resp.status_code}: {tail}")
            async for raw_line in resp.aiter_lines():
                if not raw_line:
                    continue
                if not raw_line.startswith("data:"):
                    continue
                payload = raw_line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {})
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    yield ("reasoning", reasoning)
                text = delta.get("content")
                if text:
                    yield ("content", text)
                if choices[0].get("finish_reason"):
                    # llama-server already sends [DONE] right after but
                    # break here too so we don't depend on it.
                    return

    async def complete_once(
        self,
        *,
        prompt: str,
        max_tokens: int = 1,
        temperature: float = 0.0,
    ) -> dict:
        """Fire one tiny non-streaming completion. Used by /api/warmup."""
        if not self._client:
            raise RuntimeError("llama-server client not initialised; did you await start()?")
        body = {
            "model": "minicpm",
            "prompt": prompt,
            "n_predict": int(max_tokens),
            "temperature": float(temperature),
            "stream": False,
        }
        r = await self._client.post("/completion", json=body)
        r.raise_for_status()
        return r.json()
