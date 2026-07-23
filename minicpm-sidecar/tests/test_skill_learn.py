"""Lock down the external-distillation skill surface.

Covers ``SkillService.create_skill`` (the write surface) and
``evolve.build_learn_prompt`` (the prompt builder). Together these are the
引导→创建 step of the framework's closed loop — a capability must become an
auditable text file, not a weight change.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gateway.evolve import build_learn_prompt
from gateway.skills import SkillService


@pytest.fixture
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SkillService:
    """A SkillService with its writable root pointed at a tmp dir."""
    monkeypatch.setenv("MINICPM_SKILL_DIR", str(tmp_path))
    s = SkillService()
    s.add_default_roots()
    s.refresh()
    return s


# ----------------------------------------------------------------------
# create_skill — the external-distillation write surface
# ----------------------------------------------------------------------

def test_create_skill_writes_and_is_discovered(svc: SkillService, tmp_path: Path):
    r = svc.create_skill(
        "test-learn-flow",
        "把调试流程固化成可复用技能。",
        "# Test\n\n## 何时用\n\n出错时。\n",
        tags=["test", "learn"],
    )
    assert r["ok"] is True
    assert r["slug"] == "test-learn-flow"
    # File landed in the writable root with the slug as filename.
    assert (tmp_path / "test-learn-flow.md").exists()
    # Refresh picks it up — /api/skills would now list it.
    svc.refresh()
    names = [s["name"] for s in svc.list_skills()]
    assert "test-learn-flow" in names


def test_create_skill_frontmatter_round_trips(svc: SkillService):
    """What create_skill writes must parse cleanly back via SkillInfo.load."""
    svc.create_skill(
        "round-trip",
        "Description with a: colon gets quoted.",
        "# Body\n\ncontent here",
    )
    svc.refresh()
    detail = svc.get_skill("round-trip")
    assert detail is not None
    assert detail["content"].startswith("# Body")
    assert "content here" in detail["content"]


def test_create_skill_refuses_clobber(svc: SkillService):
    """A second create with the same name must NOT silently overwrite."""
    svc.create_skill("dup-check", "first", "# first")
    r = svc.create_skill("dup-check", "second", "# second")
    assert r["ok"] is False
    assert "already exists" in r["error"]
    # Original intact.
    svc.refresh()
    detail = svc.get_skill("dup-check")
    assert "first" in detail["description"]


def test_create_skill_overwrite_flag(svc: SkillService):
    """overwrite=True replaces the file."""
    svc.create_skill("ow-skill", "v1", "# v1")
    r = svc.create_skill("ow-skill", "v2", "# v2", overwrite=True)
    assert r["ok"] is True
    svc.refresh()
    detail = svc.get_skill("ow-skill")
    assert "v2" in detail["description"]


def test_create_skill_slugifies_name(svc: SkillService):
    """Non-filesystem-safe names slugify for the filename, keep frontmatter name."""
    r = svc.create_skill("My Cool Skill!", "desc.", "# body")
    assert r["ok"] is True
    assert r["slug"] == "my-cool-skill"
    svc.refresh()
    # Frontmatter name is the user's intent; discovery keys by it.
    assert svc.get_skill("My Cool Skill!") is not None


def test_create_skill_requires_name_description_body(svc: SkillService):
    assert svc.create_skill("", "d", "b")["ok"] is False
    assert svc.create_skill("n", "", "b")["ok"] is False
    assert svc.create_skill("n", "d", "   ")["ok"] is False


def test_create_skill_description_with_colon_is_quoted(svc: SkillService):
    """A colon in description must be YAML-safe (quoted), not corrupt the file."""
    svc.create_skill("colon-test", "Goal: do X safely.", "# body")
    svc.refresh()
    detail = svc.get_skill("colon-test")
    # If the quote broke, _parse_frontmatter would mis-read description.
    assert detail is not None
    assert "do X safely" in detail["description"]


# ----------------------------------------------------------------------
# build_learn_prompt — the 引导 step
# ----------------------------------------------------------------------

def test_learn_prompt_empty_input_falls_back_to_conversation():
    """No explicit request → prompt defaults to "the workflow we just did"."""
    p = build_learn_prompt("")
    assert "[/learn]" in p
    assert "刚才" in p or "这段对话" in p  # conversation-fallback phrasing


def test_learn_prompt_embeds_user_request():
    p = build_learn_prompt("把调试 renderer 的流程固化，关注 console 报错")
    assert "调试 renderer" in p
    assert "[/learn]" in p


def test_learn_prompt_tells_model_to_use_skill_create():
    """The prompt must instruct the model to save via the skill_create tool."""
    p = build_learn_prompt("anything")
    assert "skill_create" in p


def test_learn_prompt_carries_authoring_standards():
    """The HARDLINE authoring rules must be in the prompt, not omitted."""
    p = build_learn_prompt("x")
    # Frontmatter rule + body section rule must both appear.
    assert "frontmatter" in p.lower() or "name" in p
    assert "何时用" in p  # the body section order section
    assert "description" in p.lower()


def test_learn_prompt_no_markup_invention():
    """Prompt must not invent commands the pet doesn't have.

    The pet's tool surface is memory/observe_screen/screen_click/skill_create —
    NOT hermes' terminal/read_file/skill_manage. The port must have swapped
    those out (skill_create, not skill_manage).
    """
    p = build_learn_prompt("x")
    assert "skill_manage" not in p  # hermes tool name must be gone
    assert "skill_create" in p     # replaced by the pet's tool


# ----------------------------------------------------------------------
# e2e: learn prompt → create_skill → discovery
# ----------------------------------------------------------------------

def test_learn_to_create_to_discover_round_trip(svc: SkillService):
    """The closed loop: prompt builds, a skill created from its guidance,
    discovered and served."""
    prompt = build_learn_prompt("把'重启 sidecar'流程固化成技能")
    assert "[/learn]" in prompt
    # Simulate the model authoring the skill the prompt asked for:
    r = svc.create_skill(
        "restart-sidecar",
        "安全重启推理 sidecar 的步骤。",
        "# 重启 Sidecar\n\n## 步骤\n\n1. 停止\n2. 重启\n",
        tags=["sidecar", "ops"],
    )
    assert r["ok"] is True
    svc.refresh()
    detail = svc.get_skill("restart-sidecar")
    assert detail is not None
    assert "停止" in detail["content"]
