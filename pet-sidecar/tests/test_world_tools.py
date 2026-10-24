"""刚体选择能力 tests — pet_world_walk_to 的位移算术 + 视锥选择列表.

模型只负责选 id；走到哪里由 `_walk_to_delta` 算（纯函数，DIP 单位），
看得见什么由 `_format_bodies_choice` 决定（与潜意识同一条视锥规则）。
"""

from __future__ import annotations

from gateway.server import _format_bodies_choice, _walk_to_delta


PET = {"x": 100, "y": 400, "width": 120, "height": 120}   # 中心 (160, 460)
BODY = {"id": 3, "label": "记事本", "rect": {"left": 400, "top": 200, "right": 600, "bottom": 500}}


# ── _walk_to_delta：旁边 / 站上去 ─────────────────────────────────
def test_beside_from_left():
    # 猫在刚体左边 → 停到刚体左缘外 gap
    dx, dy = _walk_to_delta(PET, BODY, stand=False)
    # 目标中心 x = 400 - 28 - 60 = 312 → dx = 312 - 160 = 152
    # 目标中心 y = 500 - 60 = 440 → dy = 440 - 460 = -20
    assert (dx, dy) == (152, -20)


def test_beside_from_right_approaches_right_edge():
    pet_right = {"x": 700, "y": 400, "width": 120, "height": 120}  # 中心 (760, 460)
    dx, dy = _walk_to_delta(pet_right, BODY, stand=False)
    # 目标中心 x = 600 + 28 + 60 = 688 → dx = 688 - 760 = -72
    assert dx == -72
    assert dy == -20


def test_stand_lands_on_top_centered():
    dx, dy = _walk_to_delta(PET, BODY, stand=True)
    # 目标中心 x = (400+600)/2 = 500 → dx = 340；y = 200 - 60 = 140 → dy = -320
    assert (dx, dy) == (340, -320)


def test_zero_delta_when_already_there():
    pet_there = {"x": 252, "y": 380, "width": 120, "height": 120}  # 中心 (312, 440) = beside 目标
    dx, dy = _walk_to_delta(pet_there, BODY, stand=False)
    assert (dx, dy) == (0.0, 0.0)


def test_bad_geometry_returns_none():
    assert _walk_to_delta({}, BODY) is None
    assert _walk_to_delta(PET, {"id": 1}) is None
    assert _walk_to_delta(PET, {"id": 1, "rect": {"left": "x"}}) is None


# ── _format_bodies_choice：只列看得见的 ───────────────────────────
def _items():
    return [
        {"id": 1, "label": "VSCode", "dist": 210.0, "angle": 0.1, "in_fov": True},
        {"id": 2, "label": "回收站", "dist": 90.0, "angle": -0.05, "in_fov": True},
        {"id": 3, "label": "背后的大窗口", "dist": 300.0, "angle": 3.0, "in_fov": False},
    ]


def test_lists_visible_sorted_by_distance_and_hides_rest():
    text = _format_bodies_choice(_items(), "right")
    assert "id=2" in text and "id=1" in text
    assert "id=3" not in text
    assert text.index("id=2") < text.index("id=1")  # 近的排前面
    assert "1 个在视野外" in text
    assert "自己挑" in text


def test_all_hidden_reports_count_only():
    text = _format_bodies_choice([{"id": 9, "label": "X", "dist": 500.0, "angle": 3.1, "in_fov": False}], "right")
    assert "视野内没有刚体" in text and "1 个在视野外" in text


def test_no_bodies_at_all():
    assert "还没有刚体" in _format_bodies_choice([], "right")


def test_direction_words_from_angle():
    text = _format_bodies_choice(_items(), "right")
    # angle≈0 的应该带「正前方」或类似方位词（vision.label_for_angle 的输出）
    assert "px" in text
