"""Speech impulse (说话冲动) — the subconscious fallback scheduler for
proactive chat.

理想状态下，模型每轮自己在回复末尾输出 [NEXT_CHAT:秒数]——那是意识层
在决定"我什么时候想再说话"（包括 [NEXT_CHAT:0] = 我决定保持沉默）。
但小模型经常忘记输出这个标签，于是"我决定不说"退化成"我忘了决定"，
桌宠从此永远沉默。

这个模块是潜意识兜底：当模型没有给出时间，系统按它此刻的状态决定
说话间隙——

- 心情 (emotion_index)：开心/兴奋 → 更想分享，间隙缩短；
  难过/冷静 → 更收敛，间隙拉长。
- 冷场轮数 (proactive_streak)：连续几次主动开口都没得到回应 →
  越等越久（1.6 倍/次，封顶 5 次）。用户一开口即清零。
- 回忆次数 (recall_count)：本会话里成功想起旧事越多 → 心绪越活跃，
  轻微缩短间隙。
- 直觉压力 (intuition_pressure)：决策循环刚浮现的强内心声音会轻轻
  把下一次开口提前；用户已经回应后压力归零。压力只缩短、不决定
  说什么或做什么。

设计边界：模型永远可以显式选择沉默——[NEXT_CHAT:0] 原样转发，永不
兜底。兜底只发生在"忘了决定"的时候；系统决定的是"什么时候开口"，
不是"必须说什么"。
"""

from __future__ import annotations

import math

# ── Tuning constants ─────────────────────────────────────────────────

# Moderate default: the model's own tags in practice range 60-3600s;
# the subconscious default sits at "会惦记你，但不烦你".
BASE_GAP_SECONDS = 1200  # 20 min

MIN_GAP_SECONDS = 180        # 3 min — never nag
MAX_GAP_SECONDS = 3 * 3600   # 3 h — never vanish

# Mood modulation (asymmetric: sadness slows more than joy speeds up —
# 低落的人不是"稍微不想说话"，是需要更久才想开口).
MOOD_HAPPY_SCALE = 0.35  # index > 0: gap × (1 − 0.35·index)
MOOD_SAD_SCALE = 0.50    # index < 0: gap × (1 + 0.50·|index|)

# Consecutive unanswered proactive attempts → exponential withdrawal.
PROACTIVE_BACKOFF = 1.6
PROACTIVE_BACKOFF_CAP = 5  # ×1.6^5 ≈ 10.5 at most

# Recent successful recalls → the mind is stirring, slightly sooner.
RECALL_EASE = 0.04
RECALL_EASE_CAP = 5

# A fresh inner voice (flash of intuition) may pull the next opening
# earlier. It only ever shortens — at full pressure it removes at most
# INTUITION_EASE of the wait. It never chooses an action or wording.
INTUITION_EASE = 0.35


def _clamp_pressure(value: float) -> float:
    """Defensive [0, 1] clamp: bad/NaN/out-of-range input means no pressure."""
    try:
        pressure = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(pressure):
        return 0.0
    return max(0.0, min(1.0, pressure))


def compute_gap(
    emotion_index: float = 0.0,
    proactive_streak: int = 0,
    recall_count: int = 0,
    intuition_pressure: float = 0.0,
) -> int:
    """Compute the seconds until the next proactive chat attempt.

    Pure function of the pet's current subconscious state — no I/O, no
    randomness (the randomness lives in the model's own tags; the
    subconscious should be steady and predictable).

    `intuition_pressure` comes from the decision loop's latest strong
    inner voice. It scales the gap down by at most INTUITION_EASE and is
    applied before the min/max clamp, so fear of nagging still wins.
    """
    idx = max(-1.0, min(1.0, emotion_index or 0.0))
    gap = float(BASE_GAP_SECONDS)

    if idx > 0:
        gap *= 1.0 - MOOD_HAPPY_SCALE * idx
    elif idx < 0:
        gap *= 1.0 + MOOD_SAD_SCALE * (-idx)

    streak = max(0, min(PROACTIVE_BACKOFF_CAP, int(proactive_streak or 0)))
    gap *= PROACTIVE_BACKOFF ** streak

    rec = max(0, min(RECALL_EASE_CAP, int(recall_count or 0)))
    gap *= 1.0 - RECALL_EASE * rec

    gap *= 1.0 - INTUITION_EASE * _clamp_pressure(intuition_pressure)

    return int(max(MIN_GAP_SECONDS, min(MAX_GAP_SECONDS, round(gap))))
