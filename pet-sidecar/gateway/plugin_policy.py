"""Static policy for self-evolved plugins (audit V-1).

The pet writes its own organs: `plugin_forge` saves a .py into
`memories/plugins/` and the hot loader runs it with `compile()` + `exec()`
in the gateway process, at the user's privilege level. That is the feature —
and it is also the shortest path from "some text on the pet's screen" to
"arbitrary code on the user's machine", because everything the model reads
(OCR of the screen, MCP tool results, a cloud provider's reply) can be
authored by somebody else.

This module is the gate in front of `exec()`. It is a **policy check, not a
sandbox**: a determined author with a Python escape hatch can still get
around an AST scan, and the honest long-term fix is out-of-process execution.
What it does buy is the realistic case — a prompt-injected model that would
gladly write `import os; os.system(...)` if the tool let it — while staying
invisible to real organs. The ten organs shipping today import only
`gateway.*`, `datetime`, `threading`, `asyncio` and `types`.

Two rules, in order of how much they cost a legitimate author:
  1. imports come from an allowlist (an organ is memory + tools, not an app);
  2. the classic sandbox-escape surfaces are refused outright: `eval`/`exec`/
     `compile`/`open`/`getattr`, and dunder attribute traversal like
     `__globals__` / `__subclasses__`.
"""

from __future__ import annotations

import ast
from typing import Iterable, List, Set

# Roots an organ may import. Everything here is pure computation or an
# existing gateway module; nothing here can reach the filesystem, the network
# or another process.
ALLOWED_IMPORT_ROOTS: Set[str] = {
    "gateway",
    "asyncio",
    "collections",
    "copy",
    "dataclasses",
    "datetime",
    "enum",
    "functools",
    "itertools",
    "json",
    "math",
    "random",
    "re",
    "string",
    "textwrap",
    "threading",
    "time",
    "types",
    "typing",
    "unicodedata",
}

# Sub-modules that look harmless under an allowed root but are not.
DENIED_IMPORTS: Set[str] = {
    "gateway.server",  # reaching back into the app would bypass this check
}

# Builtins that hand over code execution, the filesystem, or an object's
# internals.
DENIED_CALLS: Set[str] = {
    "__import__",
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "exit",
    "globals",
    "input",
    "locals",
    "memoryview",
    "open",
    "quit",
    "vars",
}

# Attribute-by-name accessors. These are how a sandbox escape is usually
# spelled (`getattr(obj, "__glo" + "bals__")`), but organs legitimately use
# them to probe an injected service (`getattr(mood, "emotion_index", None)`),
# so they are allowed only with a literal, non-dangerous attribute name.
NAME_ACCESSORS: Set[str] = {"getattr", "setattr", "delattr"}
# (call node) -> index of the argument holding the attribute name.
ACCESSOR_NAME_ARG = {"getattr": 1, "setattr": 1, "delattr": 1}

# Attributes used to walk from a safe object to a dangerous one.
DENIED_ATTRS: Set[str] = {
    "__annotations__",
    "__bases__",
    "__builtins__",
    "__class__",
    "__code__",
    "__dict__",
    "__globals__",
    "__loader__",
    "__mro__",
    "__reduce__",
    "__reduce_ex__",
    "__spec__",
    "__subclasses__",
    "__getattribute__",
    "__setattr__",
    "cr_frame",
    "gi_frame",
    "f_builtins",
    "f_globals",
}


def _import_roots(node: ast.Import) -> Iterable[str]:
    for alias in node.names:
        yield alias.name.split(".")[0]


def _module_path(node: ast.AST) -> str:
    """Dotted path of an `ast.Attribute` chain, e.g. `a.b.c`."""
    parts: List[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def check_source(source: str) -> List[str]:
    """Return policy violations for one plugin's source (empty == allowed)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        # The loader reports syntax errors anyway; surfacing it here keeps the
        # forge tool's answer identical whether the code failed to parse or
        # failed the policy.
        return [f"syntax error: {exc.msg} (line {exc.lineno})"]

    violations: List[str] = []

    def deny(node: ast.AST, message: str) -> None:
        violations.append(f"line {getattr(node, 'lineno', '?')}: {message}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for root in _import_roots(node):
                if root not in ALLOWED_IMPORT_ROOTS:
                    deny(node, f"import of '{root}' is not allowed for plugins")
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: no absolute root to check
                deny(node, "relative imports are not allowed for plugins")
                continue
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORT_ROOTS:
                deny(node, f"import of '{root}' is not allowed for plugins")
            elif _module_path_from_module(node) in DENIED_IMPORTS:
                deny(node, f"importing '{_module_path_from_module(node)}' is not allowed")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in DENIED_CALLS:
                deny(node, f"call to '{func.id}' is not allowed for plugins")
            elif isinstance(func, ast.Name) and func.id in NAME_ACCESSORS:
                name_arg = node.args[ACCESSOR_NAME_ARG[func.id]] if len(node.args) > ACCESSOR_NAME_ARG[func.id] else None
                literal = name_arg.value if isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str) else None
                if literal is None:
                    deny(node, f"{func.id}() needs a literal attribute name (a computed one is how a sandbox escape is spelled)")
                elif literal in DENIED_ATTRS:
                    deny(node, f"{func.id}(..., '{literal}') is not allowed for plugins")
            elif isinstance(func, ast.Attribute) and func.attr in DENIED_ATTRS:
                deny(node, f"access to '{func.attr}' is not allowed for plugins")
        elif isinstance(node, ast.Attribute):
            if node.attr in DENIED_ATTRS:
                deny(node, f"access to '{node.attr}' is not allowed for plugins")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                if name.startswith("__"):
                    deny(node, f"access to dunder '{name}' is not allowed for plugins")

    # One pass may flag the same attribute twice (as a load and as a store);
    # keep the order stable but drop exact repeats.
    seen: Set[str] = set()
    unique: List[str] = []
    for item in violations:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _module_path_from_module(node: ast.ImportFrom) -> str:
    return node.module or ""


def format_violations(violations: List[str], limit: int = 6) -> str:
    shown = "; ".join(violations[:limit])
    if len(violations) > limit:
        shown += f"; (+{len(violations) - limit} more)"
    return shown
