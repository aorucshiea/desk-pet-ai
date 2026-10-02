"""MemoryFiles — the model-owned 「记忆」 folder sandbox.

The captain's redesign (2026-10-02): the fixed MEMORY.md / USER.md pair
is gone; the model organizes its own memory tree (subfolders + .md
files, or a root 记忆.md). These tests pin the sandbox: traversal and
non-.md rejection, folder auto-creation, caps, tree listing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.memory.files import MemoryFiles


@pytest.fixture
def files(tmp_path: Path) -> MemoryFiles:
    return MemoryFiles(tmp_path / "mem")


def test_root_folder_created_on_init(tmp_path: Path):
    f = MemoryFiles(tmp_path / "mem")
    assert f.root.is_dir()
    assert f.root.name == "记忆"


def test_write_read_roundtrip(files: MemoryFiles):
    r = files.write("记忆.md", "# 我之所以是我\n……")
    assert r["success"] is True and r["note"] == "created"
    r = files.read("记忆.md")
    assert r["success"] is True and "我之所以是我" in r["content"]


def test_write_creates_parent_folders(files: MemoryFiles):
    r = files.write("技能/调试心得.md", "先复现再改")
    assert r["success"] is True
    assert (files.root / "技能" / "调试心得.md").is_file()


def test_sanitize_rejects_traversal(files: MemoryFiles):
    for bad in ["../escape.md", "..\\escape.md", "C:/evil.md", "a/../../b.md", "..", "", None]:
        assert files.sanitize(bad) is None, bad
    assert not (files.root.parent / "escape.md").exists()


def test_sanitize_rejects_non_md_and_hidden(files: MemoryFiles):
    assert files.sanitize("note.txt") is None
    assert files.sanitize(".hidden.md") is None
    assert files.sanitize("正常/文件.md") == "正常/文件.md"


def test_read_missing_file_is_recoverable(files: MemoryFiles):
    r = files.read("不存在.md")
    assert r["success"] is False and "no such" in r["error"]


def test_delete(files: MemoryFiles):
    files.write("旧.md", "x")
    assert files.delete("旧.md")["success"] is True
    assert files.delete("旧.md")["success"] is False


def test_write_rejects_empty_and_oversize(files: MemoryFiles):
    assert files.write("空.md", "   ")["success"] is False
    assert files.write("大.md", "x" * 40_000)["success"] is False


def test_tree_sorted_and_counted(files: MemoryFiles):
    files.write("b.md", "x")
    files.write("技能/a.md", "x")
    assert files.tree() == ["b.md", "技能/a.md"]
    assert files.count() == 2


def test_read_truncates_oversize(files: MemoryFiles):
    files.write("大.md", "字" * 20_000)
    r = files.read("大.md")
    assert r["success"] is True and "截断" in r["content"]
