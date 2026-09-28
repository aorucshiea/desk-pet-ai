"""Speech impulse (说话冲动) tests — the subconscious fallback that
decides the next proactive-chat gap when the model forgets to emit
[NEXT_CHAT:N].

Key semantic: an explicit [NEXT_CHAT:0] (model chose silence) is
forwarded untouched and NEVER reaches this module; the impulse only
fills the "forgot to decide" gap.
"""

from __future__ import annotations

from gateway.memory.impulse import (
    BASE_GAP_SECONDS,
    MAX_GAP_SECONDS,
    MIN_GAP_SECONDS,
    compute_gap,
)


def test_baseline_is_the_default_gap():
    assert compute_gap() == BASE_GAP_SECONDS
    assert compute_gap(emotion_index=0.0, proactive_streak=0, recall_count=0) == BASE_GAP_SECONDS


def test_happy_shortens_sad_lengthens():
    happy = compute_gap(emotion_index=0.8)
    sad = compute_gap(emotion_index=-0.8)
    assert happy < BASE_GAP_SECONDS
    assert sad > BASE_GAP_SECONDS
    # Asymmetric: sadness slows more than joy speeds up.
    assert (sad - BASE_GAP_SECONDS) > (BASE_GAP_SECONDS - happy)


def test_proactive_streak_backs_off_exponentially():
    gaps = [compute_gap(proactive_streak=n) for n in range(6)]
    assert all(gaps[i] < gaps[i + 1] for i in range(len(gaps) - 1))
    # Capped: streak 5 and streak 9 are the same (no runaway silence).
    assert compute_gap(proactive_streak=5) == compute_gap(proactive_streak=9)
    assert compute_gap(proactive_streak=5) <= MAX_GAP_SECONDS


def test_recall_count_eases_the_gap():
    assert compute_gap(recall_count=5) < BASE_GAP_SECONDS
    assert compute_gap(recall_count=50) == compute_gap(recall_count=5)  # capped


def test_always_within_bounds_and_never_negative():
    extremes = [
        compute_gap(emotion_index=1.0, proactive_streak=0, recall_count=5),
        compute_gap(emotion_index=-1.0, proactive_streak=5, recall_count=0),
        compute_gap(emotion_index=0.7, proactive_streak=2, recall_count=3),
    ]
    for gap in extremes:
        assert MIN_GAP_SECONDS <= gap <= MAX_GAP_SECONDS


def test_combined_state_still_sane():
    # Sad + talking into the void for a while → long, near-max wait.
    assert compute_gap(emotion_index=-0.6, proactive_streak=4) > BASE_GAP_SECONDS * 2
    # Happy + mind stirring → shorter than base but still ≥ min.
    gap = compute_gap(emotion_index=0.6, proactive_streak=0, recall_count=5)
    assert MIN_GAP_SECONDS <= gap < BASE_GAP_SECONDS
