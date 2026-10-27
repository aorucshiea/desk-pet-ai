"""capture_screen defaults to the pet's EXISTING vision cone.

The cone itself lives in gateway.decision.vision (±30°, 400px); the
screenshot helper only mirrors that geometry onto the primary display.
No new vision system, no scale plumbing: physical scale comes from
image.width / workArea.width because pet bounds are DIP.
"""

from __future__ import annotations

import base64
import io
import logging

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from gateway import server as server_mod
from gateway.server import crop_to_vision_cone


def _scene_b64(width: int = 1000, height: int = 600) -> str:
    """Quadrants: TL red, TR green, BL blue, BR yellow."""
    img = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    hw, hh = width // 2, height // 2
    draw.rectangle([0, 0, hw - 1, hh - 1], fill=(255, 0, 0))
    draw.rectangle([hw, 0, width - 1, hh - 1], fill=(0, 255, 0))
    draw.rectangle([0, hh, hw - 1, height - 1], fill=(0, 0, 255))
    draw.rectangle([hw, hh, width - 1, height - 1], fill=(255, 255, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _decode(b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


def _reddish(pixel) -> bool:
    r, g, b = pixel
    return r > 150 and g < 100 and b < 100


def _greenish(pixel) -> bool:
    r, g, b = pixel
    return g > 150 and r < 100 and b < 100


def _black(pixel) -> bool:
    return max(pixel) < 40


_WA = {"x": 0, "y": 0, "width": 1000, "height": 600}


def test_facing_right_keeps_front_and_blacks_side():
    b64 = _scene_b64()
    pet = {"x": 100, "y": 260, "width": 100, "height": 80}  # eye (150, 300)

    out = crop_to_vision_cone(b64, pet, "right", _WA)

    assert out is not None
    img = _decode(out)
    # Sector bbox only: x 150..550, y 100..500 — far-left/behind is gone.
    assert img.size == (400, 400)
    assert img.size[0] * img.size[1] < 1000 * 600
    # (400, 250) is ahead: red stays red-ish. Crop offset (150, 100).
    assert _reddish(img.getpixel((250, 150)))
    # (160, 110) is inside the bbox but behind/above the cone: black.
    assert _black(img.getpixel((10, 10)))


def test_facing_left_mirrors_the_cone():
    b64 = _scene_b64()
    pet = {"x": 800, "y": 260, "width": 100, "height": 80}  # eye (850, 300)

    out = crop_to_vision_cone(b64, pet, "left", _WA)

    assert out is not None
    img = _decode(out)
    assert img.size == (400, 400)
    # (600, 250) is ahead of a left-facing pet: green stays green-ish.
    assert _greenish(img.getpixel((150, 150)))
    # (840, 110) is behind the pet: black.
    assert _black(img.getpixel((390, 10)))


def test_unusable_geometry_returns_none():
    b64 = _scene_b64()
    pet = {"x": 100, "y": 260, "width": 100, "height": 80}

    assert crop_to_vision_cone(b64, pet, "right", {}) is None
    assert crop_to_vision_cone(b64, pet, "right", {"width": 0}) is None
    assert crop_to_vision_cone(b64, {}, "right", _WA) is None
    # Pet lives on another display / outside the screenshot.
    assert crop_to_vision_cone(b64, {**pet, "x": 5000}, "right", _WA) is None


class _Eye:
    name = "fake-eye"


def _make_client(monkeypatch):
    monkeypatch.setenv("PET_SCREEN_CONSENT", "always")
    app = server_mod.build_app(initial_model=None)
    return TestClient(app)


def _install_fakes(monkeypatch, view):
    monkeypatch.setattr(server_mod, "screen_capture", lambda: _scene_b64())
    monkeypatch.setattr(server_mod, "_active_chat_provider", lambda: _Eye())
    monkeypatch.setattr(server_mod, "_screen_tool_can_see_images", lambda server: True)

    async def _fake_view():
        return view

    monkeypatch.setattr(server_mod, "_pet_view_for_capture", _fake_view)


def _call_capture(client: TestClient, arguments: dict) -> dict:
    resp = client.post(
        "/api/mcp/execute",
        json={"server_name": "builtin", "tool_name": "capture_screen", "arguments": arguments},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert not data.get("is_error"), data
    return data


def test_capture_screen_defaults_to_cone(monkeypatch):
    client = _make_client(monkeypatch)
    _install_fakes(monkeypatch, {"pet": {"x": 100, "y": 260, "width": 100, "height": 80},
                                 "facing": "right", "wa": _WA})

    data = _call_capture(client, {})

    assert data["summary"] == "Screenshot captured (vision cone)."
    assert _decode(data["screenshot_base64"]).size == (400, 400)


def test_capture_screen_full_skips_cone(monkeypatch):
    client = _make_client(monkeypatch)
    _install_fakes(monkeypatch, {"pet": {}, "facing": "right", "wa": _WA})
    monkeypatch.setattr(
        server_mod,
        "_pet_view_for_capture",
        lambda: pytest.fail("full=true must not fetch the pet view"),
    )

    data = _call_capture(client, {"full": True})

    assert data["summary"] == "Screenshot captured (full screen)."
    # Existing whole-screen path: 1000x600 -> 768x460.
    assert _decode(data["screenshot_base64"]).size == (768, 460)


def test_capture_screen_falls_back_to_full_screen(monkeypatch, caplog):
    client = _make_client(monkeypatch)
    _install_fakes(monkeypatch, None)
    monkeypatch.setattr(logging.getLogger("pet.gateway"), "propagate", True)

    with caplog.at_level(logging.WARNING, logger="pet.gateway"):
        data = _call_capture(client, {})

    assert data["summary"] == "Screenshot captured (full screen)."
    assert _decode(data["screenshot_base64"]).size == (768, 460)
    assert "cone crop unavailable, using full screen" in caplog.text


def test_capture_screen_schema_mentions_cone_and_full(monkeypatch):
    client = _make_client(monkeypatch)
    tools = client.get("/api/mcp/servers").json()["tools"]
    tool = next(
        t for t in tools
        if t["server_name"] == "builtin" and t["name"] == "capture_screen"
    )

    assert "vision cone" in tool["description"]
    assert "full=true" in tool["description"]
    full = tool["input_schema"]["properties"]["full"]
    assert full["type"] == "boolean"
    assert "explicitly" in full["description"]
