"""S5: llama.cpp engine self-update (SidecarUpdater).

Locks down:
  - version parsing (local --version output, GitHub tag)
  - platform → asset mapping
  - check() comparison (older local → available)
  - apply() download → verify → swap with .bak backup + rollback
    (file swap is tested with a mock release asset + monkeypatched
    httpx.stream; the real llama-server is never touched)
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from gateway.sidecar_updater import (
    SidecarUpdater,
    parse_build_version,
    parse_tag_build,
)


class TestParse:
    def test_build_version(self):
        assert parse_build_version("version: 9371 (f12cc6d0f)") == 9371
        assert parse_build_version("version: 10217 (abc)") == 10217

    def test_build_version_garbage(self):
        assert parse_build_version("llama-server v0.0.0") is None
        assert parse_build_version("") is None
        assert parse_build_version(None) is None

    def test_tag_build(self):
        assert parse_tag_build("b10217") == 10217
        assert parse_tag_build("b9371") == 9371

    def test_tag_garbage(self):
        assert parse_tag_build("v1.2.3") is None
        assert parse_tag_build("") is None


class TestAssetMapping:
    @pytest.fixture
    def updater(self, tmp_path):
        u = SidecarUpdater(install_root=tmp_path)
        u._backend = "cpu"
        return u

    def test_win_x64_cpu(self, updater, monkeypatch):
        monkeypatch.setattr("gateway.sidecar_updater.platform.system", lambda: "Windows")
        monkeypatch.setattr("gateway.sidecar_updater.platform.machine", lambda: "AMD64")
        assert updater.asset_name("b10217") == "llama-b10217-bin-win-cpu-x64.zip"

    def test_win_x64_vulkan(self, updater, monkeypatch):
        updater._backend = "vulkan"
        monkeypatch.setattr("gateway.sidecar_updater.platform.system", lambda: "Windows")
        monkeypatch.setattr("gateway.sidecar_updater.platform.machine", lambda: "AMD64")
        assert updater.asset_name("b10217") == "llama-b10217-bin-win-vulkan-x64.zip"

    def test_win_arm64_falls_back_to_cpu(self, updater, monkeypatch):
        updater._backend = "vulkan"  # official has no win vulkan arm64
        monkeypatch.setattr("gateway.sidecar_updater.platform.system", lambda: "Windows")
        monkeypatch.setattr("gateway.sidecar_updater.platform.machine", lambda: "ARM64")
        assert updater.asset_name("b10217") == "llama-b10217-bin-win-cpu-arm64.zip"

    def test_mac_arm64(self, updater, monkeypatch):
        monkeypatch.setattr("gateway.sidecar_updater.platform.system", lambda: "Darwin")
        monkeypatch.setattr("gateway.sidecar_updater.platform.machine", lambda: "arm64")
        assert updater.asset_name("b10217") == "llama-b10217-bin-macos-arm64.tar.gz"

    def test_linux_vulkan(self, updater, monkeypatch):
        updater._backend = "vulkan"
        monkeypatch.setattr("gateway.sidecar_updater.platform.system", lambda: "Linux")
        monkeypatch.setattr("gateway.sidecar_updater.platform.machine", lambda: "x86_64")
        assert updater.asset_name("b10217") == "llama-b10217-bin-ubuntu-vulkan-x64.tar.gz"


class TestCheck:
    def test_available_when_remote_newer(self, tmp_path, monkeypatch):
        u = SidecarUpdater(install_root=tmp_path)
        monkeypatch.setattr(u, "local_version", lambda *a, **k: 9371)
        monkeypatch.setattr(u, "remote_version", lambda *a, **k: "b10217")
        r = u.check()
        assert r["available"] is True
        assert r["local_build"] == 9371
        assert r["remote_build"] == 10217

    def test_up_to_date(self, tmp_path, monkeypatch):
        u = SidecarUpdater(install_root=tmp_path)
        monkeypatch.setattr(u, "local_version", lambda *a, **k: 10217)
        monkeypatch.setattr(u, "remote_version", lambda *a, **k: "b10217")
        assert u.check()["available"] is False

    def test_remote_failure_reports_error_not_crash(self, tmp_path, monkeypatch):
        u = SidecarUpdater(install_root=tmp_path)
        monkeypatch.setattr(u, "local_version", lambda *a, **k: 9371)
        monkeypatch.setattr(u, "remote_version", lambda *a, **k: None)
        r = u.check()
        assert r["available"] is False
        assert r["error"] is not None


class TestApplySwap:
    """File-swap behaviour with a mock release asset + fake httpx stream."""

    def _make_zip(self, build_output: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("llama-server.exe", "fake binary")
            zf.writestr("llama.dll", "fake lib")
        return buf.getvalue()

    def _updater(self, tmp_path: Path) -> SidecarUpdater:
        root = tmp_path / "install"
        root.mkdir()
        (root / "llama-server.exe").write_text("old engine", encoding="utf-8")
        (root / "llama.dll").write_text("old lib", encoding="utf-8")
        return SidecarUpdater(install_root=root)

    def test_apply_swaps_files_and_backs_up(self, tmp_path, monkeypatch):
        u = self._updater(tmp_path)
        payload = self._make_zip("fake")

        class FakeResp:
            headers = {"content-length": str(len(payload))}

            def raise_for_status(self):
                pass

            def iter_bytes(self, size):
                yield payload

        class Ctx:
            def __enter__(self):
                return FakeResp()

            def __exit__(self, *a):
                return False

        def fake_stream(method, url, **kw):
            return Ctx()

        monkeypatch.setattr("gateway.sidecar_updater.httpx.stream", fake_stream)
        monkeypatch.setattr(u, "remote_version", lambda *a, **k: "b10217")
        monkeypatch.setattr(
            "gateway.sidecar_updater.platform.system", lambda: "Windows"
        )
        monkeypatch.setattr(
            "gateway.sidecar_updater.platform.machine", lambda: "AMD64"
        )
        monkeypatch.setattr(u, "_verify_binary", lambda *a, **k: True)

        phases = [ev["phase"] for ev in u.apply()]
        assert phases == ["start", "transfer", "swap", "complete"]

        # New files in place, old ones backed up then cleaned.
        assert (u.install_root / "llama-server.exe").read_text(encoding="utf-8") == "fake binary"
        assert not (u.install_root / ".bak").exists()
        assert not list(u.install_root.parent.glob("*.engine-staging"))

    def test_apply_verify_failure_aborts_without_touching_install(self, tmp_path, monkeypatch):
        u = self._updater(tmp_path)
        payload = self._make_zip("fake")

        class FakeResp:
            headers = {"content-length": "0"}

            def raise_for_status(self):
                pass

            def iter_bytes(self, size):
                yield payload

        class Ctx:
            def __enter__(self):
                return FakeResp()

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("gateway.sidecar_updater.httpx.stream", lambda *a, **kw: Ctx())
        monkeypatch.setattr(u, "remote_version", lambda *a, **k: "b10217")
        monkeypatch.setattr(
            "gateway.sidecar_updater.platform.system", lambda: "Windows"
        )
        monkeypatch.setattr(
            "gateway.sidecar_updater.platform.machine", lambda: "AMD64"
        )
        monkeypatch.setattr(u, "_verify_binary", lambda *a, **k: False)

        phases = [ev["phase"] for ev in u.apply()]
        assert phases == ["start", "transfer", "swap", "error"]
        # Install root untouched.
        assert (u.install_root / "llama-server.exe").read_text(encoding="utf-8") == "old engine"

    def test_apply_callbacks_stop_and_start(self, tmp_path, monkeypatch):
        """stop_callback before swap, start_callback after (bridged by
        the caller into its event loop)."""
        u = self._updater(tmp_path)
        payload = self._make_zip("fake")

        class FakeResp:
            headers = {"content-length": "0"}

            def raise_for_status(self):
                pass

            def iter_bytes(self, size):
                yield payload

        class Ctx:
            def __enter__(self):
                return FakeResp()

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("gateway.sidecar_updater.httpx.stream", lambda *a, **kw: Ctx())
        monkeypatch.setattr(u, "remote_version", lambda *a, **k: "b10217")
        monkeypatch.setattr(
            "gateway.sidecar_updater.platform.system", lambda: "Windows"
        )
        monkeypatch.setattr(
            "gateway.sidecar_updater.platform.machine", lambda: "AMD64"
        )
        monkeypatch.setattr(u, "_verify_binary", lambda *a, **k: True)

        events = []
        list(u.apply(
            stop_callback=lambda: events.append("stop"),
            start_callback=lambda: events.append("start"),
        ))
        assert events == ["stop", "start"]

    def test_busy_lock_rejects_concurrent_apply(self, tmp_path):
        u = self._updater(tmp_path)
        u._busy = True
        phases = [ev["phase"] for ev in u.apply()]
        assert phases == ["error"]


class TestApplyFromDir:
    """Offline update from a user-provided engine folder."""

    def _updater(self, tmp_path: Path) -> SidecarUpdater:
        root = tmp_path / "install"
        root.mkdir()
        (root / "llama-server.exe").write_text("old engine", encoding="utf-8")
        return SidecarUpdater(install_root=root)

    def _source_dir(self, tmp_path: Path, build_output: str) -> Path:
        # The "binary" is a placeholder file — the version check runs it
        # through subprocess.run which the tests mock.
        src = tmp_path / "engine-copy"
        src.mkdir()
        (src / "llama-server.exe").write_text("placeholder", encoding="utf-8")
        (src / "llama.dll").write_text("new lib", encoding="utf-8")
        return src

    def _mock_version(self, monkeypatch, build_output: str):
        class FakeResult:
            stdout = f"version: {build_output} (abc)"
            stderr = ""
        monkeypatch.setattr(
            "gateway.sidecar_updater.subprocess.run",
            lambda *a, **k: FakeResult(),
        )

    def test_offline_update_swaps_and_keeps_source(self, tmp_path, monkeypatch):
        u = self._updater(tmp_path)
        src = self._source_dir(tmp_path, "10218")
        self._mock_version(monkeypatch, "10218")
        monkeypatch.setattr(u, "local_version", lambda *a, **k: 9371)
        monkeypatch.setattr(
            "gateway.sidecar_updater.platform.system", lambda: "Windows"
        )
        events = list(u.apply_from_dir(str(src)))
        phases = [ev["phase"] for ev in events]
        assert phases == ["start", "verify", "swap", "complete"]
        assert events[1]["build"] == 10218
        # Install root replaced; source folder untouched (copy, not move).
        assert (u.install_root / "llama.dll").read_text(encoding="utf-8") == "new lib"
        assert (src / "llama-server.exe").exists()
        assert (src / "llama.dll").exists()

    def test_older_folder_rejected(self, tmp_path, monkeypatch):
        u = self._updater(tmp_path)
        src = self._source_dir(tmp_path, "9000")
        self._mock_version(monkeypatch, "9000")
        monkeypatch.setattr(u, "local_version", lambda *a, **k: 9371)
        monkeypatch.setattr(
            "gateway.sidecar_updater.platform.system", lambda: "Windows"
        )
        events = list(u.apply_from_dir(str(src)))
        assert events[0]["phase"] == "start"
        assert events[-1]["phase"] == "error"
        assert "不高于当前" in events[-1]["message"]
        # Install untouched.
        assert (u.install_root / "llama-server.exe").read_text(encoding="utf-8") == "old engine"

    def test_missing_folder_error(self, tmp_path):
        u = self._updater(tmp_path)
        events = list(u.apply_from_dir(str(tmp_path / "nope")))
        assert events[-1]["phase"] == "error"

    def test_callbacks_stop_start(self, tmp_path, monkeypatch):
        u = self._updater(tmp_path)
        src = self._source_dir(tmp_path, "10218")
        self._mock_version(monkeypatch, "10218")
        monkeypatch.setattr(u, "local_version", lambda *a, **k: 9371)
        monkeypatch.setattr(
            "gateway.sidecar_updater.platform.system", lambda: "Windows"
        )
        events = []
        list(u.apply_from_dir(
            str(src),
            stop_callback=lambda: events.append("stop"),
            start_callback=lambda: events.append("start"),
        ))
        assert events == ["stop", "start"]
