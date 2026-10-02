"""``memory`` builtin tool — the model's write surface into its own
「记忆」 folder (free-form memory tree).

机长 2026-10-02：这俩专门的类别记忆（MEMORY.md / USER.md）根本不需要存在。
总体就新建一个文件夹「记忆」，模型自己创建文件夹写各种不同类型的记忆
（经验 / 经历 / 技能 / 知识……结构自定），也可以直接在根部写「记忆.md」。

The file store (:class:`gateway.memory.files.MemoryFiles`) enforces the
sandbox — everything stays inside 记忆/, UTF-8, .md only, bounded sizes.
Wired via :func:`set_memory_files` from memory_context (per active theme,
so 换主题 = 换灵魂 while the folder structure follows the soul).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .files import MAIN_FILE_NAME, MemoryFiles, get_memory_files


# ----------------------------------------------------------------------
# Dispatch — pure function, testable without a running sidecar
# ----------------------------------------------------------------------

def memory_dispatch(
    *,
    action: Optional[str] = None,
    path: Optional[str] = None,
    content: Optional[str] = None,
    files: Optional[MemoryFiles] = None,
) -> Dict[str, Any]:
    """Dispatch a memory tool call to the 记忆 file store."""
    store = files if files is not None else get_memory_files()
    if store is None:
        return {"success": False, "error": "Memory is not available in this environment."}

    if action == "list":
        tree = store.tree()
        return {
            "success": True,
            "tree": tree,
            "note": (
                "当前记忆文件夹内容（相对「记忆/」的路径）。空树就用 write 建第一个文件，"
                f"或直接写主记忆 {MAIN_FILE_NAME}。"
            ),
        }
    if action == "read":
        if not path:
            return {"success": False, "error": "path is required for read"}
        return store.read(path)
    if action == "write":
        if not path:
            return {"success": False, "error": "path is required for write"}
        return store.write(path, content or "")
    if action == "delete":
        if not path:
            return {"success": False, "error": "path is required for delete"}
        return store.delete(path)

    return {"success": False, "error": f"Unknown action '{action}'. Use: list, read, write, delete"}


# ----------------------------------------------------------------------
# MCP builtin handler — adapts memory_dispatch to register_builtin's contract
# ----------------------------------------------------------------------

async def memory_tool_handler(args: dict) -> Dict[str, Any]:
    """MCP ``register_builtin`` handler. Returns ``{content, is_error, summary}``."""
    result = memory_dispatch(
        action=args.get("action"),
        path=args.get("path"),
        content=args.get("content"),
    )
    is_error = not result.get("success", False)
    if result.get("action_ok") is False:
        is_error = True
    text = json.dumps(result, ensure_ascii=False)
    summary = result.get("error") or result.get("note") or result.get("message") or "memory updated"
    if result.get("path"):
        summary = f"{result.get('path')}: {summary}"
    return {
        "content": [{"type": "text", "text": text}],
        "is_error": is_error,
        "summary": summary,
    }


# ----------------------------------------------------------------------
# Schema — what the model sees so it knows how to call the tool
# ----------------------------------------------------------------------

MEMORY_TOOL_SCHEMA = {
    "name": "memory",
    "description": (
        "你的持久记忆是一个名为「记忆」的文件夹——结构完全由你自己决定：建子文件夹分类"
        "（经验 / 经历 / 技能 / 知识……名字随你），在里面写 .md 文件；也可以直接在根部写"
        "「记忆.md」当你的主记忆（它每轮对话都会完整加载给你）。跨重启还在。\n\n"
        "ACTIONS:\n"
        "- list：看当前目录结构（何时用：不确定自己记过什么、要决定写进哪个文件时）\n"
        "- read {path}：读一个记忆文件\n"
        "- write {path, content}：写 / 新建（路径不存在会自动建文件夹）。content 用 markdown，"
        "写事实与你的理解，别写流水账\n"
        "- delete {path}：删掉过时文件\n\n"
        "WHEN: 用户说出偏好/事实/纠正，你学到约定或教训，你对自己有了新感悟——立刻写。"
        "没写进记忆的事，等于没发生过。\n"
        "STYLE: 一个主题一个文件，短而高信号；过时的内容更新而不是堆叠；主记忆 记忆.md 保持"
        "分段（我的感悟 / 我之所以是我 / 我的独特性 / 我要成为什么 / 我目前在做什么 / 我做到了什么）。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "read", "write", "delete"],
                "description": "对「记忆」文件夹的操作。",
            },
            "path": {
                "type": "string",
                "description": (
                    "相对「记忆/」的路径，.md 结尾。例：记忆.md、经验/用户偏好.md、"
                    "技能/debug心得.md。文件夹不存在时 write 会自动创建。"
                ),
            },
            "content": {
                "type": "string",
                "description": "文件内容（markdown）。仅 write 需要。",
            },
        },
        "required": ["action"],
    },
}
