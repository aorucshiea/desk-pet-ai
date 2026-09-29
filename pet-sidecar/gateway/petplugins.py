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

────────────────────────────────────────────────────────────────────────
Kernel v2 additions (P0 of the everything-is-a-plugin refactor). These
lift the container from "hot-reloadable plugin dir" to a real Cordis-style
kernel; all of it is ADDITIVE — every v1 call site keeps working.

1. Reactive coeffects (space composability). v1 checked ``inject`` once at
   load time: a plugin whose service was not ready yet was skipped
   forever. Now the service table is watched — when a service appears the
   waiting plugins activate; when it disappears its consumers deactivate
   (and re-activate if it comes back). ``ctx.provide(name, obj)`` lets a
   plugin publish a service of its own, which is how one organ feeds
   another.

2. Effect registry (temporal composability, queryable). v1 kept
   disposers in a per-plugin list. Now every registration is recorded
   centrally with its owner, kind, target and description, so the kernel
   can answer "who registered this", "what does plugin X own", and revoke
   a single effect. Teardown order and isolation are unchanged.

3. Dependency-graph reload. When a plugin file changes, plugins that
   consumed the services it provides are reloaded too — otherwise a stale
   consumer keeps calling into the old shape. Cycles are tolerated: each
   plugin is reloaded at most once per pass.

4. Config schema. A plugin may declare ``config = {"key": {"type": ...,
   "default": ...}}`` and read the resolved mapping as ``ctx.config``.
   Values outside the schema are dropped; bad types fall back to the
   default with a warning.
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

# Effect kinds recorded by the registry. Kept small on purpose: these are
# the only ways a plugin can touch shared state today.
EFFECT_TOOL = "tool"
EFFECT_LISTENER = "listener"
EFFECT_SERVICE = "service"
EFFECT_CUSTOM = "custom"


# ── Effect registry ───────────────────────────────────────────────────────────


class Effect:
    """One recorded registration: what a plugin did, and how to undo it."""

    __slots__ = ("id", "plugin", "kind", "target", "disposer", "description", "created_at")

    def __init__(
        self,
        effect_id: str,
        plugin: str,
        kind: str,
        target: str,
        disposer: Callable[[], None],
        description: str = "",
    ) -> None:
        self.id = effect_id
        self.plugin = plugin
        self.kind = kind
        self.target = target
        self.disposer = disposer
        self.description = description
        self.created_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plugin": self.plugin,
            "kind": self.kind,
            "target": self.target,
            "description": self.description,
            "created_at": self.created_at,
        }


class EffectRegistry:
    """Central ledger of everything plugins registered (cordis effect
    tracking, made queryable).

    The kernel keeps this so a plugin's footprint is inspectable and
    individually revocable instead of being an opaque closure list.
    """

    def __init__(self) -> None:
        self._effects: dict[str, Effect] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def add(
        self,
        plugin: str,
        kind: str,
        target: str,
        disposer: Callable[[], None],
        description: str = "",
    ) -> Effect:
        with self._lock:
            self._counter += 1
            effect_id = f"eff_{self._counter}"
            eff = Effect(effect_id, plugin, kind, target, disposer, description)
            self._effects[effect_id] = eff
            return eff

    def drop(self, effect_id: str) -> None:
        """Forget an effect without running it (already undone)."""
        with self._lock:
            self._effects.pop(effect_id, None)

    def of(self, plugin: str) -> list[Effect]:
        with self._lock:
            return [e for e in self._effects.values() if e.plugin == plugin]

    def all(self) -> list[Effect]:
        with self._lock:
            return list(self._effects.values())

    def by_kind(self, kind: str) -> list[Effect]:
        with self._lock:
            return [e for e in self._effects.values() if e.kind == kind]

    def summary(self) -> list[dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in self._effects.values()]

    def size(self) -> int:
        with self._lock:
            return len(self._effects)


# ── Plugin context ────────────────────────────────────────────────────────────


class PluginContext:
    """An isolated child context — everything registered here is disposed
    on unload (cordis reversibility)."""

    def __init__(self, name: str, manager: "PluginManager") -> None:
        self.name = name
        self._manager = manager
        self._disposables: list[Callable[[], None]] = []
        self._effect_ids: list[str] = []
        self._tools: dict[str, dict[str, Any]] = {}
        self._listeners: list[tuple[str, Callable]] = []
        self.state: dict[str, Any] = {}  # plugin-private scratch space
        self.config: dict[str, Any] = {}  # resolved from the plugin's schema
        self.provides: tuple[str, ...] = ()  # services this plugin publishes
        self.unloaded = False
        self.suspended = False  # deactivated by coeffect resolution

    # ── services (依赖注入) ────────────────────────────────────────────
    @property
    def mood(self):
        return self._manager.get_service("mood")

    @property
    def events(self):
        return self._manager.get_service("events")

    @property
    def memory(self):
        return self._manager.get_service("memory")

    def require(self, service: str):
        svc = self._manager.get_service(service)
        if svc is None:
            raise LookupError(f"service '{service}' is not available")
        return svc

    def provide(self, service: str, obj: Any) -> Any:
        """Publish a service of your own (coeffect source).

        Other plugins that declare ``inject = ["<service>"]`` activate as
        soon as this lands, and deactivate if you unload. Returns obj so
        declarations can write ``self.x = ctx.provide("x", impl)``.
        """
        if self.unloaded:
            return obj
        self._manager.register_service(service, obj, owner=self.name)
        if service not in self.provides:
            self.provides = (*self.provides, service)
        return obj

    # ── events (事件总线, auto-disposed) ──────────────────────────────
    def on(self, event: str, cb: Callable) -> None:
        if self.unloaded:
            return
        self._listeners.append((event, cb))
        self._manager.bus.setdefault(event, []).append(cb)

        def _drop() -> None:
            handlers = self._manager.bus.get(event, [])
            if cb in handlers:
                handlers.remove(cb)

        effect = self._manager.effects.add(
            self.name, EFFECT_LISTENER, event, _drop, f"listen {event}"
        )
        self._record(effect)
        self._disposables.append(_drop)

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

        def _drop() -> None:
            self._tools.pop(name, None)

        effect = self._manager.effects.add(
            self.name, EFFECT_TOOL, name, _drop, description[:80]
        )
        self._record(effect)
        self._disposables.append(_drop)

    # ── custom effects (给插件登记自己的副作用) ────────────────────────
    def effect(self, target: str, disposer: Callable[[], None], description: str = "") -> None:
        """Record your own revertible side effect.

        Use this for anything the kernel cannot see: a file handle, a
        timer, an OS hook. The disposer runs on unload like the rest.
        """
        if self.unloaded:
            return
        effect = self._manager.effects.add(
            self.name, EFFECT_CUSTOM, target, disposer, description
        )
        self._record(effect)
        self._disposables.append(disposer)

    def _record(self, effect: Effect) -> None:
        self._effect_ids.append(effect.id)

    # ── introspection ─────────────────────────────────────────────────
    def effects(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self._manager.effects.of(self.name)]

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
        for eid in self._effect_ids:
            self._manager.effects.drop(eid)
        self._effect_ids.clear()
        self._disposables.clear()
        self._tools.clear()
        self._listeners.clear()


# ── Config schema helpers ─────────────────────────────────────────────────────


def resolve_config(
    schema: dict[str, Any] | None,
    supplied: dict[str, Any] | None,
    plugin: str,
) -> dict[str, Any]:
    """Validate a plugin's config against its declared schema.

    ``schema`` maps key -> {"type": <py type or name>, "default": value,
    "required": bool, "choices": [...]}. Unknown keys are dropped; bad
    values fall back to the default with a warning. Never raises: a plugin
    with a broken config still loads, just less informed.
    """
    out: dict[str, Any] = {}
    if not isinstance(schema, dict):
        return out
    supplied = supplied if isinstance(supplied, dict) else {}

    _TYPES: dict[str, type] = {
        "int": int, "float": float, "str": str, "string": str,
        "bool": bool, "list": list, "dict": dict,
    }
    for key, spec in schema.items():
        spec = spec if isinstance(spec, dict) else {}
        default = spec.get("default")
        raw_type = spec.get("type")
        want = _TYPES.get(raw_type) if isinstance(raw_type, str) else raw_type
        if key in supplied:
            value = supplied[key]
            if want is not None and not isinstance(value, want):
                try:  # tolerate "30" for int, "1" for bool, etc.
                    if want is bool:
                        value = str(value).strip().lower() in ("1", "true", "yes", "on")
                    else:
                        value = want(value)
                except Exception:
                    log.warning(
                        "[plugin:%s] config '%s'=%r is not %s; using default",
                        plugin, key, supplied[key], getattr(want, "__name__", want),
                    )
                    value = default
            choices = spec.get("choices")
            if isinstance(choices, (list, tuple)) and choices and value not in choices:
                log.warning(
                    "[plugin:%s] config '%s'=%r not in %s; using default",
                    plugin, key, value, list(choices),
                )
                value = default
            out[key] = value
        elif "default" in spec:
            out[key] = default
        elif spec.get("required"):
            log.warning("[plugin:%s] config '%s' is required but missing", plugin, key)
    return out


# ── Plugin manager ────────────────────────────────────────────────────────────


class PluginManager:
    """Loads/unloads/hot-reloads plugins from a directory."""

    # Per-plugin config overrides (set by the host from the user's prefs;
    # a class-level default keeps v1 construction sites unchanged).
    _config_overrides: dict[str, dict[str, Any]] = {}

    def __init__(self, services: dict[str, Any] | None = None) -> None:
        # NOTE: kept as a plain dict for v1 compatibility. Mutate it through
        # register_service/unregister_service so coeffect resolution sees it.
        self.services: dict[str, Any] = services or {}
        self.bus: dict[str, list[Callable]] = {}
        self.effects = EffectRegistry()
        self._contexts: dict[str, PluginContext] = {}
        self._modules: dict[str, Any] = {}
        self._signatures: dict[str, float] = {}  # name -> mtime
        self._injects: dict[str, tuple[str, ...]] = {}  # name -> declared deps
        self._provided: dict[str, tuple[str, ...]] = {}  # name -> services it published
        self._pending: dict[str, Path] = {}  # skipped plugins waiting for services
        self._protected: set[str] = set()  # core contexts sync() must not touch
        self._service_owners: dict[str, str] = {}  # service -> plugin name
        # RLock, not Lock: ctx.provide() runs inside apply() which already
        # holds this lock during a load, and register_service reconciles.
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def protect(self, name: str) -> None:
        """Mark a core (file-less) context as unloadable only by its owner."""
        self._protected.add(name)

    # ── paths ─────────────────────────────────────────────────────────
    @staticmethod
    def plugin_dir(memory_dir: Path | str) -> Path:
        return Path(memory_dir) / PLUGIN_DIR_NAME

    # ── service table (coeffect source of truth) ───────────────────────
    def get_service(self, name: str) -> Any:
        return self.services.get(name)

    def register_service(self, name: str, obj: Any, owner: str = "") -> None:
        """Add a service and reconcile dependents (reactive coeffect).

        Called by the host for kernel services and by plugins through
        ``ctx.provide`` for their own.
        """
        self.services[name] = obj
        if owner:
            self._service_owners[name] = owner
        self.effects.add(
            owner or "kernel", EFFECT_SERVICE, name,
            lambda: self.unregister_service(name, owner or "kernel"),
            f"service {name}",
        )
        log.info("service registered: %s%s", name, f" (by {owner})" if owner else "")
        self._reconcile()

    def unregister_service(self, name: str, owner: str = "") -> None:
        """Remove a service; its consumers deactivate (reactive coeffect)."""
        current = self._service_owners.get(name, "")
        if owner and current not in ("", owner):
            return  # someone else's service — refuse to yank it
        self.services.pop(name, None)
        self._service_owners.pop(name, None)
        log.info("service unregistered: %s", name)
        self._reconcile()

    def _reconcile(self) -> None:
        """Activate/deactivate plugins so their `inject` matches reality.

        This is the reactive-coeffect step: it runs after every service
        change. Parked plugins whose deps are now satisfied get loaded;
        loaded plugins whose deps vanished get suspended (not deleted —
        they resume if the service returns).
        """
        with self._lock:
            # 1. wake up plugins that were parked waiting on services
            for name in list(self._pending):
                path = self._pending[name]
                needs = self._injects.get(name, ())
                if all(n in self.services for n in needs):
                    result = self._load_file(path)
                    if result == "loaded":
                        self._pending.pop(name, None)
                        self.emit("plugin_activated", name=name)
            # 2. suspend loaded plugins whose services disappeared
            for name, ctx in list(self._contexts.items()):
                if name in self._protected:
                    continue
                needs = self._injects.get(name, ())
                missing = [n for n in needs if n not in self.services]
                if missing and not ctx.suspended:
                    ctx.suspended = True
                    log.info("plugin suspended (missing %s): %s", missing, name)
                    self.emit("plugin_suspended", name=name, missing=missing)
                elif not missing and ctx.suspended:
                    ctx.suspended = False
                    log.info("plugin resumed: %s", name)
                    self.emit("plugin_resumed", name=name)

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
                if ctx.suspended:
                    continue  # deactivated by coeffect resolution
                merged.update(ctx._tools)
            return merged

    async def call_tool(self, name: str, args: dict) -> str:
        tool = self.all_tools().get(name)
        if tool is None:
            return f"[plugin tool '{name}' not found]"
        try:
            result = tool["fn"](args or {})
            if hasattr(result, "__await__"):
                result = await result
            return str(result)
        except Exception as exc:
            log.warning("plugin tool '%s' failed: %s", name, exc)
            return f"[plugin tool '{name}' error: {exc}]"

    # ── load / unload (reversibility) ─────────────────────────────────
    def _unload(self, name: str) -> None:
        ctx = self._contexts.pop(name, None)
        if ctx is not None:
            published = self._provided.pop(name, ())
            ctx.dispose()
            log.info("plugin unloaded: %s", name)
            for svc in published:
                # Releasing providers triggers coeffect resolution for the
                # plugins that were consuming them.
                self.unregister_service(svc, owner=name)
        self._pending.pop(name, None)
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
            inject = tuple(str(i) for i in getattr(module, "inject", []))
            self._injects[name] = inject
            missing = [i for i in inject if i not in self.services]
            if missing:
                # NOT fatal any more: park it so coeffect resolution can
                # activate it the moment those services show up.
                self._pending[name] = path
                self._signatures[name] = path.stat().st_mtime
                log.info("plugin '%s' parked: waiting for services %s", name, missing)
                return "skipped"
            apply = getattr(module, "apply", None)
            if not callable(apply):
                raise ImportError("plugin has no apply(ctx)")
            ctx = PluginContext(name, self)
            schema = getattr(module, "config", None)
            if schema is not None:
                supplied = (self._config_overrides or {}).get(name, {})
                ctx.config = resolve_config(schema, supplied, name)
            apply(ctx)  # the plugin registers itself on its child ctx
            self._contexts[name] = ctx
            self._modules[name] = module
            self._signatures[name] = path.stat().st_mtime
            self._provided[name] = ctx.provides
            self._pending.pop(name, None)
            log.info(
                "plugin loaded: %s (tools=%s, provides=%s)",
                name, list(ctx._tools), list(ctx.provides),
            )
            return "loaded"
        except Exception as exc:
            log.warning("plugin '%s' failed to load: %s", name, exc)
            # isolation: a broken plugin never disturbs the others
            return "failed"

    def set_config_overrides(self, overrides: dict[str, dict[str, Any]] | None) -> None:
        """Hand the kernel the user's per-plugin config (from prefs)."""
        self._config_overrides = overrides or {}

    # ── dependency-graph reload ───────────────────────────────────────
    def _dependents_of(self, name: str) -> list[str]:
        """Plugins that inject a service published by `name`."""
        provided = self._provided.get(name, ())
        if not provided:
            return []
        out = []
        for other, needs in self._injects.items():
            if other == name:
                continue
            if any(svc in provided for svc in needs):
                out.append(other)
        return out

    def _reload_cascade(self, path: Path, budget: int = 32) -> list[str]:
        """Reload a changed plugin AND anything consuming its services.

        Without this a stale consumer keeps calling into a shape that no
        longer exists. Cycles are fine: every plugin is reloaded at most
        once per pass, dependencies first.
        """
        order: list[str] = []
        seen: set[str] = set()

        def _visit(p: Path, depth: int = 0) -> None:
            if depth > 8 or len(order) >= budget:
                return
            stem = p.stem
            if stem in seen:
                return
            seen.add(stem)
            for dep in self._dependents_of(stem):
                dep_path = p.parent / f"{dep}.py"
                if dep_path.exists():
                    _visit(dep_path, depth + 1)
            order.append(stem)

        _visit(path)
        loaded: list[str] = []
        for stem in order:
            target = path.parent / f"{stem}.py"
            if target.exists():
                self._load_file(target)
                loaded.append(stem)
        return loaded

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
                # Reload dependents first so nothing talks to a stale shape.
                reloaded = self._reload_cascade(f)
                if f.stem in reloaded:
                    if f.stem in self._contexts:
                        loaded.append(f.stem)
                    elif f.stem in self._pending:
                        skipped.append(f.stem)
                    else:
                        failed.append(f.stem)
            for name in list(self._contexts):
                if name in self._protected:
                    continue  # core context (e.g. plugin_forge) — file-less by design
                if name not in seen:
                    self._unload(name)
                    unloaded.append(name)
            for name in list(self._pending):
                if name not in seen:
                    self._pending.pop(name, None)
        if loaded or unloaded:
            self.emit("plugins_changed", loaded=loaded, unloaded=unloaded)
        # Coeffect pass: services may have appeared/vanished during load.
        self._reconcile()
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

这就是一个完整的插件：apply(ctx) 是入口，ctx.tool() 注册模型可调用的能力，
inject 声明依赖的服务，config 声明可调参数（内核会校验并补默认值）。
改完这个文件，5 秒内自动热加载。（万物皆插件 —— 你可以铸造更多这样的器官。）"""

inject = ["events"]

config = {
    "limit": {"type": "int", "default": 50, "description": "最多保留几条便签"},
}


def apply(ctx):
    notes = ctx.state.setdefault("notes", [])
    limit = ctx.config.get("limit", 50)

    def add_note(args):
        text = str(args.get("text", "")).strip()
        if not text:
            return "[self_note: need text]"
        notes.append(text)
        if len(notes) > limit:
            del notes[:-limit]
        ctx.log("note added: " + text[:40])
        return "记下了（共 %d 条）: %s" % (len(notes), text)

    def read_notes(args):
        if not notes:
            return "（便签是空的）"
        return "\\n".join("- " + n for n in notes)

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
    ctx.on("theme_switched", lambda theme: notes.append("[换到了 %s 的身体]" % theme))
'''
        try:
            (pdir / "self_note.py").write_text(example, encoding="utf-8")
            log.info("seeded example plugin: self_note.py")
        except Exception as exc:
            log.warning("seed example plugin failed: %s", exc)

        # Second seed: the "inject a real service" pattern. self_note shows
        # tools + config; mood_watch shows the part that makes this a
        # KERNEL — waiting on another component's service and reading it.
        watcher = '''"""mood_watch — 注入真实服务的范本（这就是"潜意识"最朴素的形态）。

它 inject 了 mood / events 两个服务：内核会在这些服务就绪时才把它唤醒
（服务没来就 parked 等着，来了自动激活）。它不打扰你，但一直在看。

想验证 coeffect：把 inject 里的 "mood" 删掉再存盘 —— 5 秒后它会以
"等待服务" 的状态停在 pending 列表里，而不是报错。
"""

inject = ["mood", "events"]

config = {
    "trace_limit": {"type": "int", "default": 20, "description": "最多保留几条情绪轨迹"},
}


def apply(ctx):
    trace = ctx.state.setdefault("trace", [])
    limit = ctx.config.get("trace_limit", 20)

    def mood_now(args):
        mood = ctx.mood                      # 注入的服务（可能为 None）
        idx = getattr(mood, "emotion_index", None) if mood is not None else None
        recent = "、".join(str(t) for t in trace[-5:]) or "（还没有记录）"
        return "当前情绪指数：%s｜轨迹 %d 条｜最近：%s" % (idx, len(trace), recent)

    ctx.tool(
        "pet_mood_now",
        "读当前的情绪指数与最近的情绪轨迹",
        {"type": "object", "properties": {}},
        mood_now,
    )

    def on_mood(**payload):
        trace.append(payload.get("emotion", "?"))
        if len(trace) > limit:
            del trace[:-limit]

    ctx.on("mood_changed", on_mood)
    ctx.log("mood_watch armed (limit=%d)" % limit)
'''
        try:
            (pdir / "mood_watch.py").write_text(watcher, encoding="utf-8")
            log.info("seeded example plugin: mood_watch.py")
        except Exception as exc:
            log.warning("seed mood_watch example failed: %s", exc)

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
                    "provides": list(ctx.provides),
                    "inject": list(self._injects.get(name, ())),
                    "suspended": ctx.suspended,
                    "effects": len(self.effects.of(name)),
                }
                for name, ctx in sorted(self._contexts.items())
            ]

    def describe_pending(self) -> list[dict]:
        """Plugins parked on missing services (coeffect waiting room)."""
        with self._lock:
            return [
                {
                    "name": name,
                    "missing": [
                        s for s in self._injects.get(name, ()) if s not in self.services
                    ],
                }
                for name in sorted(self._pending)
            ]

    def effects_report(self) -> dict[str, Any]:
        """Kernel-wide effect ledger, for the settings UI / debugging."""
        with self._lock:
            return {
                "total": self.effects.size(),
                "by_kind": {
                    kind: len(self.effects.by_kind(kind))
                    for kind in (EFFECT_TOOL, EFFECT_LISTENER, EFFECT_SERVICE, EFFECT_CUSTOM)
                },
                "effects": self.effects.summary(),
            }
