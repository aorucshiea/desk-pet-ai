"""body_log.py - 身体日志：所有身体动作的自我叙事

设计（2026-10-19）：
- 记录来源：dice（AI 用决策模型掷骰子）/ intent（LLM 主动 [WALK]）/ decision（元决策点名）
- 也记录被压抑的念头（suppressed=True）——"我常想动但忍住"是自我认知素材
- 注入 system prompt：第一人称"你自己刚才做了什么"，让 LLM 认领这些动作（不梦游）
- 内存环形缓冲（最近 20 条），不进长期记忆（走路这种日常动作不值得占事件库）
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


class BodyLog:
    MAX_ITEMS = 20

    def __init__(self):
        self.items: List[Dict[str, Any]] = []

    def _age(self, t: float) -> str:
        d = max(0, int(time.time() - t))
        if d < 60:
            return f"{d}秒前"
        if d < 3600:
            return f"{d // 60}分钟前"
        return f"{d // 3600}小时前"

    def append(
        self,
        action: str,
        detail: str = "",
        source: str = "dice",
        suppressed: bool = False,
    ) -> None:
        """记录一次身体行为（或一个被压抑的念头）。"""
        self.items.append({
            "t": time.time(),
            "action": str(action)[:24],
            "detail": str(detail)[:120],
            "source": source,
            "suppressed": bool(suppressed),
        })
        self.items = self.items[-self.MAX_ITEMS:]

    def format_for_prompt(self, limit: int = 6) -> str:
        if not self.items:
            return ""
        lines: List[str] = ["【身体日志】这些是你自己刚才做的事——不是别人，是你："]
        for it in self.items[-limit:]:
            verb = "你" + {
                "walk": "走动了",
                "push": "推开了",
                "pull": "拉近了",
                "kick": "踢开了",
                "grab": "拎起了",
                "look": "转头看向",
                "speak": "开口说了",
            }.get(it["action"], f"做了{it['action']}")
            detail = f"（{it['detail']}）" if it["detail"] else ""
            if it["suppressed"]:
                lines.append(f"- {self._age(it['t'])}：你曾想{detail or '做点什么'}，但忍住了")
            else:
                lines.append(f"- {self._age(it['t'])}：{verb}{detail}")
        lines.append("你可以自然地提到它们，就像人记得自己刚才走过神来那样。")
        return "\n".join(lines)

    def recent(self, n: int = 5) -> List[Dict[str, Any]]:
        return self.items[-n:]
