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

# ── Emotion index (情绪指数) ──────────────────────────────────────────
# Every [EMOTION:xxx] tag the model emits moves the index: calm is 0,
# happy +0.1, sad -0.1, etc. The index modulates the NEXT reply's
# temperature (happy → more divergent, sad → more subdued). Unknown tags
# are ignored (idempotent).
EMOTION_INDEX_DELTAS: Dict[str, float] = {
    "excited": +0.15,
    "happy": +0.10,
    "curious": +0.05,
    "neutral": -0.05,
    "sad": -0.10,
    "scared": -0.10,
    "mad": -0.15,
}
INDEX_MIN = -1.0
INDEX_MAX = 1.0
# B-9: emotion regresses toward neutral over time — a feeling you slept on
# fades a little. ~10% per hour toward 0 (slow enough that a single strong
# emotion (-0.15 for mad) still dominates the curve for hours).
INDEX_REGRESSION_PER_HOUR = 0.90


def _clamp_index(value: float) -> float:
    return max(INDEX_MIN, min(INDEX_MAX, value))


def index_mood_phrase(index: float) -> str:
    """Map the numeric index to a felt phrase for the system prompt."""
    if index >= 0.4:
        return "高涨"
    if index >= 0.15:
        return "轻快"
    if index > -0.15:
        return "平静"
    if index > -0.4:
        return "低沉"
    return "低落"


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
        self._emotion_index: float = 0.0
        self._index_updated_at: str = datetime.now(timezone.utc).isoformat()

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
            self._emotion_index = _clamp_index(float(data.get("emotion_index", 0.0)))
            self._index_updated_at = data.get(
                "index_updated_at", datetime.now(timezone.utc).isoformat()
            )
            # B-9: regress toward neutral for the time we were offline.
            self._regress_index()
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
            "emotion_index": self._emotion_index,
            "index_updated_at": self._index_updated_at,
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

    @property
    def emotion_index(self) -> float:
        # B-9: reading the live index applies time-regression lazily, so a
        # long silent gap shows a calmer pet even before anything saves.
        self._regress_index()
        return self._emotion_index

    def _regress_index(self) -> None:
        """Mean-revert the emotion index toward 0, ~10% per hour elapsed."""
        if self._emotion_index == 0.0:
            self._index_updated_at = datetime.now(timezone.utc).isoformat()
            return
        try:
            last = datetime.fromisoformat(self._index_updated_at)
        except (TypeError, ValueError):
            self._index_updated_at = datetime.now(timezone.utc).isoformat()
            return
        elapsed_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
        # Sub-3-second gaps produce float noise without meaningful calming;
        # skipping them keeps rapid successive tags arithmetically exact.
        if elapsed_h < 0.001:
            return
        self._emotion_index = _clamp_index(
            self._emotion_index * (INDEX_REGRESSION_PER_HOUR ** elapsed_h)
        )
        if abs(self._emotion_index) < 1e-3:
            self._emotion_index = 0.0
        self._index_updated_at = datetime.now(timezone.utc).isoformat()

    def apply_emotion_tag(self, tag: str) -> float:
        """Move the emotion index by the tag's delta (model chose the tag).

        calm=0, happy +0.1, sad -0.1, … Clamped to [INDEX_MIN, INDEX_MAX],
        persisted, returns the new index.
        """
        delta = EMOTION_INDEX_DELTAS.get((tag or "").lower())
        if delta is None:
            return self._emotion_index  # unknown tag — idempotent
        self._regress_index()  # apply elapsed-time calm before the new hit
        self._emotion_index = _clamp_index(self._emotion_index + delta)
        self._index_updated_at = datetime.now(timezone.utc).isoformat()
        self.save()
        return self._emotion_index

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
            f"你当前的情绪指数：{self._emotion_index:+.2f}（{index_mood_phrase(self._emotion_index)}）。"
            f"指数越高你说话越活泼发散，越低越低沉收敛。\n\n"
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

    v2 (2026-08-01): emotion-flow encoding — every sentence of the event
    content carries a per-speaker emotion tag in （情绪） format, so
    recalling the event re-lives its emotional rhythm. The event-level
    emotion is derived by the system from the flow (aggregate_emotion),
    the model never writes a separate emotion field. The prompt must stay
    in sync with the renderer's <<<MEM>>> instruction block.
    """
    return f"""回顾刚才的对话。

你当前情绪：{current_mood}（{intensity}/100），原因：{reason}，从{since[:16]}持续到现在。

提取值得记住的事件（0-3条）。对每个事件输出：
{{
  "title": "一句话标题，不超过15字",
  "content": "用『句子（情绪）』格式重写刚才的对话——每句话标注当时的情绪",
  "weight": 1-999的整数,
  "type": "experience 或 knowledge",
  "resolved": true 或 false
}}

content 示例：
"今天用户问我星期几（平静），我说星期二（平淡），反问用户咋连星期几都不知道（疑惑）。"

情绪只能从这些词里选：平静、开心、难过、疑惑、生气、兴奋、疲惫、平淡、紧张、期待

规则：
- 已知常识不许写成事件——参数知识里本来就有，不是经历
- type=knowledge 只用于"你刚才学到了新信息"（比如用户告诉你的新东西）
- 未完成的事 resolved=false（事情还没结束，你放不下）；已结束的写 true
- 想不起原话可以概括，但每句话都必须带（情绪）

权重参考（不是规则，是你自己的判断）：
- 日常闲聊、无情感波动：100-300
- 有情绪起伏、用户分享了心事：400-700
- 改变你们关系的事、第一次、承诺、告别：800-999

你觉得重要就重要。不需要理由。

核心记忆（core）：如果你在这一轮里经历了你一辈子都不想忘的事，把它的 title 放进顶层 "core" 数组（最多7条）。放进去就几乎永远忘不掉——要非常谨慎，宁可少放。

然后评估你的心情变化。

输出JSON：
{{
  "events": [{{"title":"...","content":"...(情绪)格式...","weight":N,"type":"...","resolved":true/false}}],
  "core": ["想永远记住的事的标题"],
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
