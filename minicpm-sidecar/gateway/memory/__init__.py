"""Persistent cross-session memory for the desk pet.

Ported from hermes-agent's ``tools/memory_tool.py`` (MemoryStore). The
design is deliberately the same so behaviour and limits match a proven
implementation:

  - Two files: ``MEMORY.md`` (the pet's own notes) + ``USER.md`` (what the
    pet knows about the user). Both live under ``<userData>/memories/``.
  - **Frozen snapshot pattern**: ``load_from_disk()`` captures a snapshot
    that ``format_for_system_prompt()`` returns for system-prompt injection.
    Mid-session tool writes persist to disk immediately but do NOT mutate
    the snapshot — the system prompt stays stable for the whole session,
    preserving any provider prefix cache. The snapshot refreshes next boot.
  - Bounded by **character** limits (not tokens) so the cap is
    model-independent: memory 2200, user 1375.
  - Entries are delimited by ``\n§\n`` (section sign) and may be multiline.
  - ``add`` / ``replace`` / ``remove`` use short unique substring matching.
  - Atomic write via temp-file + ``os.replace()`` — readers always see a
    complete file, never a half-written one.

Differences from the upstream Hermes source (intentional simplifications
for the desk-pet scope):

  - No ``threat_patterns`` injection scan. The desk pet is a single-user,
    local-first companion; MEMORY.md is only ever written by the pet
    itself or edited directly by the user. (If you later expose memory to
    untrusted input, re-add a scan here.)
  - No cross-process file lock (``fcntl``/``msvcrt``). The sidecar runs
    one MemoryStore per process; concurrent writes from a second sidecar
    instance are not a supported topology. The atomic-rename write still
    keeps a single reader/writer safe.
  - No external-write approval gate (Hermes' ``_apply_write_gate``). The
    pet is interactive and local; writes apply immediately.
  - Drift detection is kept: if the on-disk file was hand-edited into a
    shape that wouldn't round-trip through our parser, we refuse to
    overwrite it and point at the ``.bak`` snapshot instead of silently
    truncating the user's edits.
"""

from __future__ import annotations

from .store import (
    ENTRY_DELIMITER,
    MemoryStore,
)

__all__ = ["ENTRY_DELIMITER", "MemoryStore"]
