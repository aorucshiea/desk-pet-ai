"""sleep_compress.py - 睡眠压缩：一天的对话 → 带标题的记忆条 + 关键词

设计（2026-10-19）：
- **裸模型调用**：system 为空，唯一指令是"压缩"，不带桌宠 persona / 记忆注入
- 权重由模型自评（1-999），产物写入潜意识记忆（EventStore）
- 关键词写入 `memories/keywords.json`，供最低概率的"关键词触发闪现"使用
- 输入是 `conversation_log` 的每日 JSONL，压缩后原日志可删（保留期由调用方决定）
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import conversation_log

COMPRESS_INSTRUCTION = (
    "这是你今天的全部对话记录。\n"
    "按你自己的判断，把它压缩成 3-8 条有标题的记忆条（每条 1-2 句），"
    "并给出你注意到的高频关键词（最多 12 个）。\n"
    "只输出 JSON，不要任何解释：\n"
    '{"memories":[{"title":"不超过10字","content":"1-2句","weight":1-999}],"keywords":["词"]}'
)


def _extract_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


async def compress_day(
    provider,
    day: Optional[str] = None,
    memory_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """读某天对话 → 裸模型压缩 → 返回记忆条 + 关键词（不落盘）。"""
    day = day or date.today().isoformat()
    if not conversation_log.valid_day(day):
        return {
            "status": "error",
            "day": day,
            "message": "invalid day (expected YYYY-MM-DD)",
            "memories": [],
            "keywords": [],
        }
    text = conversation_log.format_day(day, memory_dir)
    if not text.strip():
        return {"status": "empty", "day": day, "memories": [], "keywords": []}

    messages = [{"role": "user", "content": COMPRESS_INSTRUCTION + "\n\n" + text}]
    collected: List[str] = []
    try:
        async for ev in provider.chat(
            messages=messages,
            system="",
            tools=None,
            max_tokens=1500,
            temperature=0.3,
            top_p=0.9,
        ):
            if ev.get("type") == "delta":
                collected.append(ev.get("content", ""))
            elif ev.get("type") == "error":
                return {
                    "status": "error",
                    "day": day,
                    "message": ev.get("message") or "provider error",
                    "memories": [],
                    "keywords": [],
                }
    except Exception as exc:
        return {"status": "error", "day": day, "message": str(exc), "memories": [], "keywords": []}

    raw = "".join(collected)
    parsed = _extract_json(raw)
    if not parsed:
        return {
            "status": "error",
            "day": day,
            "message": "unparseable compress output",
            "raw": raw[:500],
            "memories": [],
            "keywords": [],
        }

    memories = parsed.get("memories") or []
    keywords = parsed.get("keywords") or []
    if not isinstance(memories, list):
        memories = []
    if not isinstance(keywords, list):
        keywords = []
    return {
        "status": "ok",
        "day": day,
        "memories": memories,
        "keywords": [str(k) for k in keywords][:16],
        "raw_len": len(raw),
    }


def _clamp_weight(value: Any) -> int:
    try:
        n = int(value)
    except Exception:
        return 300
    return max(1, min(999, n))


def persist(
    memories: List[dict],
    keywords: List[str],
    event_store,
    memory_dir: Optional[Path] = None,
    day: Optional[str] = None,
) -> Dict[str, Any]:
    """把压缩产物落盘：记忆条 → EventStore；关键词 → keywords.json。"""
    written = 0
    errors: List[str] = []
    for m in memories:
        try:
            title = str(m.get("title") or "").strip()[:40]
            content = str(m.get("content") or "").strip()[:400]
            if not title or not content:
                continue
            event_store.add_event(
                title=title,
                content=content,
                weight=_clamp_weight(m.get("weight")),
                type_="experience",
            )
            written += 1
        except Exception as exc:
            errors.append(str(exc))

    kw_written = False
    try:
        base = Path(memory_dir) if memory_dir else conversation_log.default_memory_dir()
        base.mkdir(parents=True, exist_ok=True)
        kw_path = base / "keywords.json"
        old: List[str] = []
        if kw_path.exists():
            try:
                data = json.loads(kw_path.read_text(encoding="utf-8"))
                old = [str(k) for k in (data.get("keywords") or [])]
            except Exception:
                old = []
        # 合并去重，保留最近 24 个（新词优先）
        merged: List[str] = []
        for k in list(keywords) + old:
            k = str(k).strip()
            if k and k not in merged:
                merged.append(k)
        merged = merged[:24]
        kw_path.write_text(
            json.dumps(
                {
                    "keywords": merged,
                    "day": day or date.today().isoformat(),
                    "updated": date.today().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        kw_written = True
    except Exception as exc:
        errors.append(str(exc))

    return {
        "events_written": written,
        "keywords_written": kw_written,
        "errors": errors[:5],
    }
