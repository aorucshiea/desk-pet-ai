"""Vision cone — what the pet can and cannot see."""

from __future__ import annotations

from gateway.decision.vision import bearing, label_for_angle, visible


def test_dead_ahead_and_behind():
    assert bearing(100, 0, "right") == 0
    assert abs(bearing(-100, 0, "right")) == 180
    assert visible(100, 0, "right")["in_fov"] is True
    assert visible(-100, 0, "right")["in_fov"] is False


def test_facing_left_mirrors_the_cone():
    # facing left: a body to the left is ahead, a body to the right is behind
    assert abs(bearing(-100, 0, "left")) == 0
    assert visible(-100, 0, "left")["in_fov"] is True
    assert visible(100, 0, "left")["in_fov"] is False


def test_cone_is_60_degrees_and_400px():
    # 30° is the edge (tan30 ≈ 0.577); 31° is outside
    assert visible(100, 57.7, "right")["in_fov"] is True
    assert visible(400, 0, "right")["in_fov"] is True
    assert visible(401, 0, "right")["in_fov"] is False
    assert visible(100, 200, "right")["in_fov"] is False  # ~63°


def test_labels():
    assert label_for_angle(0) == "正前方"
    assert label_for_angle(8) == "正前方"
    assert label_for_angle(20) == "右前"
    assert label_for_angle(-20) == "左前"
