"""A4: per-action screen permission (AI-agent style).

Locks down the ScreenPermissionManager: deny → notify + await user
(respond allow/deny/remember), timeout → denied, always → no prompt,
once → promoted, unknown request → error.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.screen_consent import ScreenPermissionManager


@pytest.fixture
def mgr() -> ScreenPermissionManager:
    return ScreenPermissionManager("deny")


class TestRequest:
    @pytest.mark.asyncio
    async def test_always_state_skips_prompt(self):
        m = ScreenPermissionManager("always")
        notified = []
        assert await m.request(lambda *a: notified.append(a), "截图", "x") is True
        assert notified == []  # never asked

    @pytest.mark.asyncio
    async def test_deny_notifies_and_waits(self, mgr):
        notified = []
        task = asyncio.create_task(
            mgr.request(lambda rid, tool, desc: notified.append((rid, tool, desc)), "截图", "看屏幕")
        )
        await asyncio.sleep(0.01)
        assert len(notified) == 1
        rid, tool, desc = notified[0]
        assert tool == "截图"
        assert desc == "看屏幕"
        assert mgr.pending_count == 1
        # Allow → request resolves True.
        r = await mgr.respond(rid, allow=True)
        assert r == {"ok": True, "allowed": True}
        assert await task is True
        assert mgr.pending_count == 0

    @pytest.mark.asyncio
    async def test_deny_user_denies(self, mgr):
        notified = []
        task = asyncio.create_task(
            mgr.request(lambda rid, tool, desc: notified.append(rid), "点击", "点按钮")
        )
        await asyncio.sleep(0.01)
        rid = notified[0]
        await mgr.respond(rid, allow=False)
        assert await task is False

    @pytest.mark.asyncio
    async def test_timeout_denies(self, mgr):
        notified = []
        task = asyncio.create_task(
            mgr.request(lambda rid, tool, desc: notified.append(rid), "截图", "x", timeout=0.05)
        )
        await asyncio.sleep(0.01)
        assert len(notified) == 1
        # Timeout inside request() returns False (does not raise).
        assert await task is False
        assert mgr.pending_count == 0

    @pytest.mark.asyncio
    async def test_once_promotes_to_always(self):
        m = ScreenPermissionManager("once")
        notified = []
        assert await m.request(lambda *a: notified.append(a), "截图", "x") is True
        assert notified == []
        assert m.state == "always"
        # Second call: no prompt either (already always).
        assert await m.request(lambda *a: notified.append(a), "截图", "x") is True

    @pytest.mark.asyncio
    async def test_remember_sets_always(self, mgr):
        notified = []
        task = asyncio.create_task(
            mgr.request(lambda rid, tool, desc: notified.append(rid), "截图", "x")
        )
        await asyncio.sleep(0.01)
        rid = notified[0]
        await mgr.respond(rid, allow=True, remember=True)
        assert await task is True
        assert mgr.state == "always"
        # Next request skips the prompt entirely.
        notified2 = []
        assert await mgr.request(lambda *a: notified2.append(a), "点击", "y") is True
        assert notified2 == []


class TestRespond:
    @pytest.mark.asyncio
    async def test_unknown_request(self, mgr):
        assert await mgr.respond("perm_999", allow=True) == {
            "ok": False,
            "error": "unknown or expired request",
        }

    @pytest.mark.asyncio
    async def test_double_respond_second_fails(self, mgr):
        notified = []
        task = asyncio.create_task(
            mgr.request(lambda rid, tool, desc: notified.append(rid), "截图", "x")
        )
        await asyncio.sleep(0.01)
        rid = notified[0]
        assert (await mgr.respond(rid, allow=True))["ok"] is True
        assert await task is True
        # Request already resolved → second respond rejected.
        assert (await mgr.respond(rid, allow=True))["ok"] is False
