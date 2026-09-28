"""Self-evolution module (external distillation — Phase 3).

Implements the external-distillation framework: the pet turns a workflow it
just executed (or one the user described) into a reusable, auditable text
skill — ``SKILL.md`` — loaded at runtime by the skill system. No weight
distillation, no black box: the capability is a file the user can read,
edit, and audit. This is the engineering layer of the framework's
Section 3.1 (引导→创建→索引→加载→Patch closed loop):

  - ``learn.build_learn_prompt``  — 引导: build the prompt that tells the
                                    model to author a SKILL.md from a described
                                    workflow.
  - ``SkillService.create_skill`` — 创建: the write surface
                                    (in ``gateway/skills/__init__.py``).
  - ``SkillService.refresh``      — 索引/加载: pick up new skills w/o restart.
  - ``curator`` (planned, P2)     — Patch: background review/pin/archive.

Ported from hermes-agent's ``agent/learn_prompt.py`` (the prompt-construction
pure function), adapted to the desk-pet's tool surface (``memory`` /
``skill`` / ``screen_click`` / ``observe_screen``) and Chinese-first voice.
"""

from __future__ import annotations

from .learn import build_learn_prompt
from .curator import (
    apply_automatic_transitions,
    is_paused,
    load_state,
    run_curator,
    set_paused,
    should_run_now,
)

__all__ = [
    "build_learn_prompt",
    "apply_automatic_transitions",
    "run_curator",
    "should_run_now",
    "is_paused",
    "set_paused",
    "load_state",
]
