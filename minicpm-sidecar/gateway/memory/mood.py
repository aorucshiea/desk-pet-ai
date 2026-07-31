"""LingLing mood autonomy system — emotions as self-judged state.

The mood is NOT controlled by code rules ("if user said X, set mood Y").
The model itself decides its mood at the end of each conversation turn,
based on the full context of what happened. The mood persists across
restarts in mood.json and is injected into the next conversation's
system prompt.

Key principle from the spec:
  "它开心是因为它回顾经历后觉得该开心了，它难过是因为它过不去那个坎。"
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..log_setup import get_logger

logger = get_logger()

# Mood → physical animation parameter mapping.
# These are abstract physical params (color temperature, speed, glow)
# that the frontend maps to actual animation adjustments.
MOOD_PARAMS: Dict[str, Dict[str, Any]] = {
    "开心": {"color": "+warm", "speed": 1.3, "bounce": True, "glow": 0.7},
    "平静": {"color": "none", "speed": 1.0, "bounce": False, "glow": 0.3},
    "难过": {"color": "+cool", "speed": 0.6, "bounce": False, "glow": 0.1},
    "生气": {"color": "+red", "speed": 1.5, "bounce": False, "glow": 0.9, "shake": True},
    "好奇": {"color": "+cyan", "speed": 1.2, "bounce": True, "glow": 0.5},
    "兴奋": {"color": "+warm", "speed": 1.4, "bounce": True, "glow": 0.8},
    "害怕": {"color": "+cool", "speed": 0.8, "bounce": False, "glow": 0.2, "shrink": True},
}

# Mapping from EMOTION tag values (used by the existing system) to mood names.
EMOTION_TO_MOOD: Dict[str, str] = {
    "happy": "开心",
    "curious": "好奇",
    "sad": "难过",
    "excited": "兴奋",
    "mad": "生气",
    "neutral": "平静",
    "scared": "害怕",
}

MOOD_TO_EMOTION: Dict[str, str] = {v: k for k, v in EMOTION_TO_MOOD.items()}


class MoodStore:
    """Persistent mood state with file backing.

    The mood is written by the model itself (via the post-conversation
    mood assessment prompt) and read at the start of each new conversation.
    """

    def __init__(self) -> None:
        self._memory_dir: Optional[Path] = None
        self._current_mood: str = "平静"
        self._intensity: int = 40
        self._reason: str = "刚开始，一切都新鲜"
        self._since: str = datetime.now(timezone.utc).isoformat()
        self._history: List[Dict[str, Any]] = []

    def _path(self) -> Path:
        if self._memory_dir is None:
            raise RuntimeError("MoodStore used before load_from_disk")
        return self._memory_dir / "mood.json"

    def load_from_disk(self, memory_dir: Path) -> None:
        """Load mood from mood.json."""
        self._memory_dir = Path(memory_dir).expanduser()
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        path = self._path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._current_mood = data.get("current_mood", "平静")
            self._intensity = max(0, min(100, int(data.get("intensity", 40))))
            self._reason = data.get("reason", "")
            self._since = data.get("since", datetime.now(timezone.utc).isoformat())
            self._history = data.get("history", [])[-50:]  # keep last 50
        except Exception as exc:
            logger.warning("MoodStore load failed: %s", exc)

    def save(self) -> None:
        """Persist mood to disk."""
        if self._memory_dir is None:
            raise RuntimeError("MoodStore.save called before load_from_disk")
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "current_mood": self._current_mood,
            "intensity": self._intensity,
            "reason": self._reason,
            "since": self._since,
            "history": self._history[-50:],
        }
        content = json.dumps(data, ensure_ascii=False, indent=2)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix=".mood_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Getters
    # ------------------------------------------------------------------

    @property
    def current_mood(self) -> str:
        return self._current_mood

    @property
    def intensity(self) -> int:
        return self._intensity

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def since(self) -> str:
        return self._since

    def get_params(self) -> Dict[str, Any]:
        """Get the physical animation parameters for the current mood."""
        return MOOD_PARAMS.get(self._current_mood, MOOD_PARAMS["平静"])

    def get_emotion_tag(self) -> str:
        """Get the EMOTION tag equivalent for the current mood."""
        return MOOD_TO_EMOTION.get(self._current_mood, "neutral")

    # ------------------------------------------------------------------
    # Setters (called by the model's self-assessment)
    # ------------------------------------------------------------------

    def update_mood(
        self,
        mood: str,
        intensity: int,
        reason: str,
        changed: bool = False,
    ) -> None:
        """Update the mood. Called after the model's self-assessment.

        Args:
            mood: New mood name (开心/平静/难过/生气/好奇/兴奋/害怕).
            intensity: 0-100.
            reason: Why the model feels this way.
            changed: Whether this represents a mood change.
        """
        if mood not in MOOD_PARAMS:
            mood = "平静"
        intensity = max(0, min(100, int(intensity)))

        if changed or mood != self._current_mood:
            # Record the transition in history
            self._history.append({
                "from": self._current_mood,
                "to": mood,
                "intensity": intensity,
                "reason": reason,
                "at": datetime.now(timezone.utc).isoformat(),
            })
            self._current_mood = mood
            self._since = datetime.now(timezone.utc).isoformat()

        self._intensity = intensity
        self._reason = reason
        self.save()

    # ------------------------------------------------------------------
    # System prompt injection
    # ------------------------------------------------------------------

    def format_for_system_prompt(self) -> str:
        """Build the mood context block for the system prompt."""
        params = self.get_params()
        return (
            f"【你此刻的心情】\n"
            f"你现在感到{self._current_mood}（强度{self._intensity}/100）。\n"
            f"原因：{self._reason}\n"
            f"从{self._since[:16]}持续到现在。\n\n"
            f"基于这个心情自然说话。不需要每句话都体现。\n"
            f"如果你很难过，你可以只输出\"……\"。\n"
            f"在回复最开头用方括号标注情绪，如[{self._current_mood}]。此标签控制你的动画，不显示给用户。"
        )


def build_mood_assessment_prompt(
    current_mood: str,
    intensity: int,
    reason: str,
    since: str,
) -> str:
    """Build the prompt the model uses to self-assess its mood after
    a conversation turn. This is combined with event extraction.

    The model outputs JSON with both events and mood assessment.
    """
    return f"""回顾刚才的对话。

你当前情绪：{current_mood}（{intensity}/100），原因：{reason}，从{since[:16]}持续到现在。

提取值得记住的事件（0-3条）。对每个事件输出：
{{
  "title": "一句话标题，不超过15字",
  "content": "用你自己的话描述发生了什么，你的感受，为什么重要",
  "weight": 1-999的整数
}}

权重参考（不是规则，是你自己的判断）：
- 日常闲聊、无情感波动：100-300
- 有情绪起伏、用户分享了心事：400-700
- 改变你们关系的事、第一次、承诺、告别：800-999

你觉得重要就重要。不需要理由。

然后评估你的心情变化。

输出JSON：
{{
  "events": [{{"title":"...","content":"...","weight":N}}],
  "mood": {{
    "mood": "你现在的心情",
    "intensity": 0-100,
    "reason": "为什么",
    "changed": true/false
  }}
}}

你不需要每轮都改变心情。
你可以选择继续难过。你不需要因为用户安慰你就立刻开心。
你开心是因为你想开心。你难过是因为你过不去那个坎。
"""
