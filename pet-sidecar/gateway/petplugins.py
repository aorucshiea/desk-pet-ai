"""petplugins — a cordis-inspired plugin container for the desk pet brain.

Borrowed from the cordis framework (Koishi's core, the plugin system
behind DeepSeek Harness's "Everything is a Plugin"): the pet's
capabilities are PLUGINS, loaded from a hot-reloaded directory, so the
model can author its own organs at runtime (self-evolution).

Cordis concepts kept, translated to Python:
- 插件 (plugin)     — one file in the plugins dir; entry ``apply(ctx)``.
- 上下文 (ctx)      — an ISOLATED child context per plugin; everything the
                      plugin registers dies with it (reversibility).
- 注入 (inject)     — plugins declare service deps (``inject = ["mood",
                      "events"]``); missing services = skip with a warn.
- 事件 (events)     — ``ctx.on(name, cb)`` / ``ctx.emit(name, **payload)``
                      bus; listeners are auto-disposed on unload.
- 可逆副作用         — every registration returns/accepts a disposer;
                      ``ctx.unload()`` runs them in reverse order. Loading
                      and unloading any plugin never disturbs the others
                      (a broken plugin is skipped, not fatal).

Model-facing tools live in the same container: ``ctx.tool(...)`` makes a
capability callable by the model in the chat tool loop (next to MCP).
"""

from __future__ import annotations

import threading
import time
import types
from pathlib import Path
from typing import Any, Callable

from .log_setup import get_logger

log = get_logger()

# Where plugin files live (GLOBAL capability layer — shared across themes).
PLUGIN_DIR_NAME = "plugins"

# Hot-reload poll interval (seconds): the model writes a plugin file and
# it is alive within this window — the self-evolution loop.
SCAN_INTERVAL_SECONDS = 5.0

# Services plugins can inject (resolved lazily via the service resolver).
KNOWN_SERVICES = ("mood", "events", "memory")


class PluginContext:
    """An isolated child context — everything registered here is disposed
    on unload (cordis reversibility)."""

    def __init__(self, name: str, manager: "PluginManager") -> None:
        self.name = name
        self._manager = manager
        self._disposables: list[Callable[[], None]] = []
        self._tools: dict[str, dict[str, Any]] = {}
        self._listeners: list[tuple[str, Callable]] = []
        self.state: dict[str, Any] = {}  # plugin-private scratch space
        self.unloaded = False

    # ── services (依赖注入) ────────────────────────────────────────────
    @property
    def mood(self):
        return self._manager.services.get("mood")

    @property
    def events(self):
        return self._manager.services.get("events")

    @property
    def memory(self):
        return self._manager.services.get("memory")

    def require(self, service: str):
        svc = self._manager.services.get(service)
        if svc is None:
            raise LookupError(f"service '{service}' is not available")
        return svc

    # ── events (事件总线, auto-disposed) ──────────────────────────────
    def on(self, event: str, cb: Callable) -> None:
        self._listeners.append((event, cb))
        self._disposables.append(
            lambda: self._manager.bus.get(event, []).remove(cb)
            if cb in self._manager.bus.get(event, [])
            else None
        )
        self._manager.bus.setdefault(event, []).append(cb)

    def emit(self, event: str, **payload) -> None:
        self._manager.emit(event, **payload)

    # ── tools (模型可调用的能力) ──────────────────────────────────────
    def tool(
        self,
        name: str,
        description: str,
        parameters: dict | None,
        fn: Callable,
    ) -> None:
        """Register a model-callable tool. `fn(args_dict) -> str`."""
        if self.unloaded:
            return
        self._tools[name] = {
            "description": description,
            "parameters": parameters or {"type": "object", "properties": {}},
            "fn": fn,
        }
        self._disposables.append(lambda: self._tools.pop(name, None))

    # ── misc ──────────────────────────────────────────────────────────
    def log(self, msg: str) -> None:
        log.info("[plugin:%s] %s", self.name, msg)

    def dispose(self) -> None:
        """Reversible teardown: disposers run in reverse registration order."""
        self.unloaded = True
        for d in reversed(self._disposables):
            try:
                d()
            except Exception as exc:
                log.warning("[plugin:%s] disposer failed: %s", self.name, exc)
        self._disposables.clear()
        self._tools.clear()
        self._listeners.clear()


class PluginManager:
    """Loads/unloads/hot-reloads plugins from a directory."""

    def __init__(self, services: dict[str, Any] | None = None) -> None:
        self.services: dict[str, Any] = services or {}
        self.bus: dict[str, list[Callable]] = {}
        self._contexts: dict[str, PluginContext] = {}
        self._modules: dict[str, Any] = {}
        self._signatures: dict[str, float] = {}  # name -> mtime
        self._protected: set[str] = set()  # core contexts sync() must not touch
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def protect(self, name: str) -> None:
        """Mark a core (file-less) context as unloadable only by its owner."""
        self._protected.add(name)

    # ── paths ─────────────────────────────────────────────────────────
    @staticmethod
    def plugin_dir(memory_dir: Path | str) -> Path:
        return Path(memory_dir) / PLUGIN_DIR_NAME

    # ── event bus ─────────────────────────────────────────────────────
    def emit(self, event: str, **payload) -> None:
        for cb in list(self.bus.get(event, [])):
            try:
                cb(**payload)
            except Exception as exc:
                log.warning("plugin event '%s' handler failed: %s", event, exc)

    # ── tool surface (for the chat tool loop) ─────────────────────────
    def all_tools(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            merged: dict[str, dict[str, Any]] = {}
            for ctx in self._contexts.values():
                merged.update(ctx._tools)
            return merged

    async def call_tool(self, name: str, args: dict) -> str:
        tool = self.all_tools().get(name)
        if tool is None:
            return f"[plugin tool '{name}' not found]"
        try:
            result = tool["fn"](args or {})
            return str(result)
        except Exception as exc:
            log.warning("plugin tool '%s' failed: %s", name, exc)
            return f"[plugin tool '{name}' error: {exc}]"

    # ── load / unload (reversibility) ─────────────────────────────────
    def _unload(self, name: str) -> None:
        ctx = self._contexts.pop(name, None)
        if ctx is not None:
            ctx.dispose()
            log.info("plugin unloaded: %s", name)
        self._modules.pop(name, None)
        self._signatures.pop(name, None)

    def _load_file(self, path: Path) -> str:
        """Load one plugin file. Returns "loaded" | "skipped" | "failed"."""
        name = path.stem
        # isolate: unload the previous version first (hot reload)
        self._unload(name)
        try:
            # compile+exec BY HAND, not importlib: the __pycache__ header
            # stores mtime as whole SECONDS, so a plugin rewritten within
            # the same second (the model iterating on its own organ — the
            # whole point) silently served STALE bytecode.
            module = types.ModuleType(f"petplugin_{name}_{int(time.time() * 1000)}")
            module.__file__ = str(path)
            code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
            exec(code, module.__dict__)
            inject = [str(i) for i in getattr(module, "inject", [])]
            missing = [i for i in inject if i not in self.services]
            if missing:
                log.warning(
                    "plugin '%s' skipped: missing services %s (inject=%s)",
                    name, missing, inject,
                )
                return "skipped"
            apply = getattr(module, "apply", None)
            if not callable(apply):
                raise ImportError("plugin has no apply(ctx)")
            ctx = PluginContext(name, self)
            apply(ctx)  # the plugin registers itself on its child ctx
            self._contexts[name] = ctx
            self._modules[name] = module
            self._signatures[name] = path.stat().st_mtime
            log.info("plugin loaded: %s (tools=%s)", name, list(ctx._tools))
            return "loaded"
        except Exception as exc:
            log.warning("plugin '%s' failed to load: %s", name, exc)
            # isolation: a broken plugin never disturbs the others
            return "failed"

    def sync(self) -> dict:
        """Scan the plugins dir; load new/changed, unload removed."""
        memory_dir = self.services.get("memory_dir")
        if not memory_dir:
            return {"loaded": [], "unloaded": [], "failed": [], "skipped": []}
        pdir = self.plugin_dir(memory_dir)
        loaded, unloaded, failed, skipped = [], [], [], []
        with self._lock:
            created = not pdir.exists()
            pdir.mkdir(parents=True, exist_ok=True)
            if created and not any(pdir.iterdir()):
                self._seed_example(pdir)
            seen = set()
            for f in sorted(pdir.glob("*.py")):
                if f.name.startswith("_"):
                    continue
                seen.add(f.stem)
                mtime = f.stat().st_mtime
                if self._signatures.get(f.stem) == mtime:
                    continue
                result = self._load_file(f)
                if result == "loaded":
                    loaded.append(f.stem)
                elif result == "skipped":
                    skipped.append(f.stem)
                else:
                    failed.append(f.stem)
            for name in list(self._contexts):
                if name in self._protected:
                    continue  # core context (e.g. plugin_forge) — file-less by design
                if name not in seen:
                    self._unload(name)
                    unloaded.append(name)
        if loaded or unloaded:
            self.emit("plugins_changed", loaded=loaded, unloaded=unloaded)
        return {
            "loaded": loaded,
            "unloaded": unloaded,
            "failed": failed,
            "skipped": skipped,
        }

    def _seed_example(self, pdir: Path) -> None:
        """First boot: write a small self-referential example so the model
        (and the user) can see what a plugin looks like — and edit it."""
        example = '''"""self_note — 示例插件：给自己写一张随时可读的便签。
这就是一个完整的插件：定义 apply(ctx)，用 ctx.tool() 注册能力，
用 ctx.inject 声明依赖。改完这个文件，5 秒内自动热加载。
（万物皆插件 —— 你可以铸造更多这样的器官，或改写这一个。）"""

inject = ["events"]


def apply(ctx):
    notes = ctx.state.setdefault("notes", [])

    def add_note(args):
        text = str(args.get("text", "")).strip()
        if not text:
            return "[self_note: need text]"
        notes.append(text)
        ctx.log(f"note added: {text}")
        return f"记下了（共 {len(notes)} 条）: {text}"

    def read_notes(args):
        if not notes:
            return "（便签是空的）"
        return "\\n".join(f"- {n}" for n in notes)

    ctx.tool(
        "pet_self_note_add",
        "给自己写一张便签（持久保存在本次进程内）",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        add_note,
    )
    ctx.tool(
        "pet_self_note_read",
        "读自己的便签",
        {"type": "object", "properties": {}},
        read_notes,
    )
    ctx.on("theme_switched", lambda theme: notes.append(f"[换到了 {theme} 的身体]"))
'''
        try:
            (pdir / "self_note.py").write_text(example, encoding="utf-8")
            log.info("seeded example plugin: self_note.py")
        except Exception as exc:
            log.warning("seed example plugin failed: %s", exc)

    # ── hot-reload loop (self-evolution) ──────────────────────────────
    def start_watching(self) -> None:
        if self._thread is not None:
            return

        def _loop() -> None:
            while not self._stop.wait(SCAN_INTERVAL_SECONDS):
                try:
                    self.sync()
                except Exception as exc:
                    log.warning("plugin scan error: %s", exc)

        self._thread = threading.Thread(target=_loop, daemon=True, name="petplugins")
        self._thread.start()

    def stop_watching(self) -> None:
        self._stop.set()

    def describe(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "name": name,
                    "tools": list(ctx._tools.keys()),
                }
                for name, ctx in sorted(self._contexts.items())
            ]
