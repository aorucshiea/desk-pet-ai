"""llama.cpp self-update — user-triggered inference-engine upgrade.

The GGUF *model* updater (updater.py) has a full check/download/apply
pipeline; the *engine* (llama-server binary) had none — the fetch
scripts pin a fixed release (b9371) and only run manually in dev.

This module gives the user a one-click path:

  1. check()  — compare local build (llama-server --version) against the
     latest official GitHub release (ggml-org/llama.cpp).
  2. apply()  — stream-download the platform asset, extract to staging,
     verify the extracted llama-server actually runs and reports the
     expected build (executability check beats a checksum), then swap
     the install root via a .bak backup (mirroring updater.py).

The caller (server.py) is responsible for stopping/starting the live
llama-server around the swap; this module never touches the process.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Iterable, List, Optional

import httpx

from .log_setup import get_logger

logger = get_logger()

GITHUB_RELEASES_LATEST = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
RELEASE_DOWNLOAD = "https://github.com/ggml-org/llama.cpp/releases/download"

# Suffix for in-flight download files — atomic-renamed on completion.
_PART_SUFFIX = ".part"

# Backend subdirectories that hold the real binary inside an install root.
_BACKEND_DIRS = {"vulkan", "cuda", "metal", "cpu"}


def parse_build_version(output: str) -> Optional[int]:
    """Parse llama-server --version output → build number.

    ``version: 9371 (f12cc6d0f)`` → 9371. None if unparseable.
    """
    m = re.search(r"version:\s*(\d+)", output or "")
    if m:
        return int(m.group(1))
    return None


def parse_tag_build(tag: str) -> Optional[int]:
    """Parse a release tag like ``b10217`` → 10217. Strict: only the
    ``b<digits>`` form llama.cpp uses is accepted (a ``v1.2.3`` semver
    tag must NOT be read as build 1)."""
    m = re.match(r"^b(\d+)$", tag or "")
    if m:
        return int(m.group(1))
    return None


def _atomic_move(src: Path, dst: Path) -> None:
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(str(src), str(dst))


class SidecarUpdater:
    """Check + apply updates for the bundled llama-server binary.

    ``install_root`` is the directory that gets swapped wholesale (e.g.
    ``minicpm-sidecar/bin/win-x64/`` or packaged ``sidecar-bin/``) —
    the official release assets unpack to exactly that layout
    (llama-server.exe at top, ``backends/<backend>/`` next to it).
    """

    _cache_ttl_sec = 90

    def __init__(self, install_root: Optional[Path] = None) -> None:
        self._install_root = Path(install_root).expanduser() if install_root else None
        self._backend: str = "cpu"
        self._lock = threading.Lock()
        self._busy = False
        self._local_cache: Optional[tuple[float, Optional[int]]] = None
        self._remote_cache: Optional[tuple[float, Optional[str]]] = None

    # ------------------------------------------------------------------
    # Binary / platform resolution
    # ------------------------------------------------------------------

    def resolve(self) -> Optional[Path]:
        """Resolve the install root + backend from the same candidate
        list llama_client uses, so we always replace the binary that
        will actually launch.

        Backend note: llama_client defaults Windows to "cpu" (auto), but
        a user who explicitly set MINICPM_DEVICE=vulkan/cuda (or switched
        via /api/set-device) runs the backend subdirectory binary — the
        update asset must match that backend.
        """
        from .llama_client import _candidate_binary_paths

        for p in _candidate_binary_paths():
            if not p.is_file():
                continue
            parent = p.parent
            if parent.name in _BACKEND_DIRS:
                # bin/<triple>/backends/<backend>/llama-server[.exe]
                root = parent.parent.parent
                self._backend = parent.name
            else:
                # bin/<triple>/llama-server[.exe] or <override-dir>/…
                root = parent
                self._backend = "cpu"
            if self._install_root is None:
                self._install_root = root
            break
        else:
            return None

        # Explicit backend preference (env / set-device) overrides the
        # auto default when a matching backend binary exists.
        device = os.environ.get("MINICPM_DEVICE", "auto").strip().lower()
        if device in _BACKEND_DIRS and device != "cpu":
            backend_exe = self._install_root / "backends" / device / (
                "llama-server.exe" if platform.system() == "Windows" else "llama-server"
            )
            if backend_exe.is_file():
                self._backend = device
        return self._binary_path()

    @property
    def install_root(self) -> Optional[Path]:
        return self._install_root

    @property
    def backend(self) -> str:
        return self._backend

    def _binary_path(self) -> Optional[Path]:
        # The updater may be constructed without resolve() being called
        # (server.py builds it eagerly); resolve lazily so the local
        # version is always findable.
        if self._install_root is None:
            self.resolve()
        if self._install_root is None:
            return None
        exe = "llama-server.exe" if platform.system() == "Windows" else "llama-server"
        if self._backend in _BACKEND_DIRS and self._backend != "cpu":
            cand = self._install_root / "backends" / self._backend / exe
            if cand.is_file():
                return cand
        cand = self._install_root / exe
        return cand if cand.is_file() else None

    # ------------------------------------------------------------------
    # Versions
    # ------------------------------------------------------------------

    def local_version(self, *, use_cache: bool = True) -> Optional[int]:
        """Build number of the installed llama-server."""
        if use_cache and self._local_cache:
            ts, ver = self._local_cache
            if time.time() - ts < self._cache_ttl_sec:
                return ver
        binary = self._binary_path()
        if binary is None:
            return None
        ver: Optional[int] = None
        try:
            out = subprocess.run(
                [str(binary), "--version"],
                capture_output=True, text=True, timeout=5.0,
            )
            ver = parse_build_version(out.stdout or out.stderr)
        except Exception as exc:
            logger.warning("llama-server --version failed: %s", exc)
        self._local_cache = (time.time(), ver)
        return ver

    def remote_version(self, *, use_cache: bool = True) -> Optional[str]:
        """Latest release tag (e.g. ``b10217``) from GitHub."""
        if use_cache and self._remote_cache:
            ts, tag = self._remote_cache
            if time.time() - ts < self._cache_ttl_sec:
                return tag
        tag: Optional[str] = None
        try:
            resp = httpx.get(GITHUB_RELEASES_LATEST, timeout=10.0)
            resp.raise_for_status()
            tag = str(resp.json().get("tag_name") or "")
            tag = tag or None
        except Exception as exc:
            logger.warning("GitHub release check failed: %s", exc)
        self._remote_cache = (time.time(), tag)
        return tag

    def asset_name(self, tag: str) -> Optional[str]:
        """Map this host to the official release asset name."""
        sys_name = platform.system()
        machine = platform.machine().lower()
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
        vulkan = self._backend == "vulkan"
        if sys_name == "Windows":
            # Official Windows assets: win-{vulkan,cpu}-x64.zip, win-cpu-arm64.zip
            backend = "vulkan" if (vulkan and arch == "x64") else "cpu"
            return f"llama-{tag}-bin-win-{backend}-{arch}.zip"
        if sys_name == "Darwin":
            return f"llama-{tag}-bin-macos-{arch}.tar.gz"
        if sys_name == "Linux":
            suffix = "-vulkan" if vulkan else ""
            return f"llama-{tag}-bin-ubuntu{suffix}-{arch}.tar.gz"
        logger.warning("Unsupported host for engine update: %s/%s", sys_name, machine)
        return None

    def check(self) -> dict:
        """Compare local vs remote build."""
        local = self.local_version()
        tag = self.remote_version()
        remote = parse_tag_build(tag) if tag else None
        err = None
        if tag is None:
            err = "无法连接 GitHub release 检查（离线或网络被墙）"
        available = bool(local is not None and remote is not None and remote > local)
        return {
            "available": available,
            "local_build": local,
            "remote_tag": tag,
            "remote_build": remote,
            "asset_name": self.asset_name(tag) if tag else None,
            "busy": self._busy,
            "error": err,
        }

    # ------------------------------------------------------------------
    # Apply (SSE phase generator)
    # ------------------------------------------------------------------

    def apply(
        self,
        stop_callback: Optional[Callable[[], None]] = None,
        start_callback: Optional[Callable[[], None]] = None,
    ) -> Iterable[dict]:
        """Stream-download the latest release, verify, and swap it in.

        Phases: start / transfer / swap / complete (mirrors
        ModelUpdater.apply so the Electron progress bubble works).

        On Windows a running llama-server.exe is file-locked, so the
        swap must happen while the engine is stopped. ``stop_callback``
        (sync, called right before the file swap) and ``start_callback``
        (sync, called right after, and again on rollback) let the caller
        bridge into its asyncio loop via
        ``asyncio.run_coroutine_threadsafe``. If no callbacks are given
        (tests), the swap proceeds without touching the process.
        """
        with self._lock:
            if self._busy:
                yield {"phase": "error", "message": "另一个更新正在进行"}
                return
            self._busy = True
        try:
            tag = self.remote_version(use_cache=False)
            if not tag:
                yield {"phase": "error", "message": "获取最新版本失败，请检查网络"}
                return
            asset = self.asset_name(tag)
            if not asset:
                yield {"phase": "error", "message": "当前平台没有官方发布包"}
                return
            if self._install_root is None or not self._install_root.exists():
                yield {"phase": "error", "message": f"找不到引擎安装目录: {self._install_root}"}
                return

            url = f"{RELEASE_DOWNLOAD}/{tag}/{asset}"
            staging = self._install_root.parent / f"{self._install_root.name}.engine-staging"
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)

            # 1. Download with progress.
            archive = staging / asset
            try:
                with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length") or 0)
                    yield {"phase": "start", "bytes_total": total, "asset": asset, "tag": tag}
                    done = 0
                    tmp = archive.with_suffix(archive.suffix + _PART_SUFFIX)
                    with tmp.open("wb") as f:
                        for chunk in resp.iter_bytes(1024 * 256):
                            f.write(chunk)
                            done += len(chunk)
                            yield {
                                "phase": "transfer",
                                "file": asset,
                                "bytes_done": done,
                                "bytes_total": total,
                            }
                    _atomic_move(tmp, archive)
            except Exception as exc:
                yield {"phase": "error", "message": f"下载失败: {exc}"}
                return

            # 2. Extract + verify.
            yield {"phase": "swap", "message": "解压并校验新引擎…"}
            try:
                extract_dir = staging / "extract"
                extract_dir.mkdir(parents=True, exist_ok=True)
                if archive.suffix == ".zip":
                    _safe_zip_extract(archive, extract_dir)
                else:
                    with tarfile.open(archive, "r:gz") as tf:
                        tf.extractall(extract_dir, filter="data")
                server_bin = _find_llama_server(extract_dir)
                if server_bin is None:
                    yield {"phase": "error", "message": "发布包中找不到 llama-server"}
                    return
                if not self._verify_binary(server_bin, tag):
                    yield {"phase": "error",
                           "message": f"新引擎校验失败: 期望 {tag}, 实际无法运行或版本不符"}
                    return
            except Exception as exc:
                yield {"phase": "error", "message": f"解压校验失败: {exc}"}
                return

            # 3. Swap install root ↔ .bak (atomic-ish, mirrors updater.py).
            # The engine must be stopped first (Windows file locks).
            yield from self._swap_install(
                extract_dir, stop_callback, start_callback,
                tag=tag, asset=asset,
            )
        finally:
            self._busy = False

    def apply_from_dir(
        self,
        source_dir: str,
        stop_callback: Optional[Callable[[], None]] = None,
        start_callback: Optional[Callable[[], None]] = None,
    ) -> Iterable[dict]:
        """Offline update: install the engine from a user-provided folder
        (e.g. a copy of a release someone else downloaded on a fast
        network). The folder must contain a runnable llama-server whose
        build is >= the installed one; the source folder is COPIED, never
        modified.

        Phases: start / verify / swap / complete (same contract as
        apply(), minus download).
        """
        with self._lock:
            if self._busy:
                yield {"phase": "error", "message": "另一个更新正在进行"}
                return
            self._busy = True
        try:
            src = Path(source_dir).expanduser()
            if not src.is_dir():
                yield {"phase": "error", "message": f"文件夹不存在: {src}"}
                return
            if self._install_root is None or not self._install_root.exists():
                yield {"phase": "error", "message": f"找不到引擎安装目录: {self._install_root}"}
                return

            yield {"phase": "start", "source": str(src)}

            # Verify: find llama-server, make sure it runs and is not
            # older than the installed engine.
            server_bin = _find_llama_server(src)
            if server_bin is None:
                yield {"phase": "error", "message": "该文件夹中找不到 llama-server"}
                return
            try:
                out = subprocess.run(
                    [str(server_bin), "--version"],
                    capture_output=True, text=True, timeout=10.0,
                )
            except Exception as exc:
                yield {"phase": "error", "message": f"无法运行该引擎: {exc}"}
                return
            got = parse_build_version(out.stdout or out.stderr)
            if got is None:
                yield {"phase": "error", "message": "无法解析该引擎的版本"}
                return
            local = self.local_version()
            if local is not None and got < local:
                yield {"phase": "error",
                       "message": f"该文件夹的引擎版本 (b{got}) 不高于当前 (b{local})，未更新"}
                return
            yield {"phase": "verify", "build": got}

            # Copy (NOT move) the source folder into staging so the user's
            # copy stays intact, then swap.
            staging = self._install_root.parent / f"{self._install_root.name}.engine-staging"
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            extract_dir = staging / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)
            try:
                for child in src.iterdir():
                    dst = extract_dir / child.name
                    if child.is_dir():
                        shutil.copytree(child, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(child, dst)
            except Exception as exc:
                yield {"phase": "error", "message": f"复制文件夹失败: {exc}"}
                return

            yield {"phase": "swap", "message": "替换引擎…"}
            yield from self._swap_install(
                extract_dir, stop_callback, start_callback,
                tag=f"b{got}", asset=str(src),
            )
        finally:
            self._busy = False

    def _swap_install(
        self,
        extract_dir: Path,
        stop_callback: Optional[Callable[[], None]],
        start_callback: Optional[Callable[[], None]],
        *,
        tag: str,
        asset: str,
    ) -> Iterable[dict]:
        """Swap install root ↔ .bak with rollback; engine must be stopped
        first (Windows file locks). Shared by online (apply) and offline
        (apply_from_dir) update paths. Yields error/complete phases."""
        stopped = False
        started_again = False
        backup = self._install_root.parent / f"{self._install_root.name}.bak"
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        try:
            if stop_callback is not None:
                stop_callback()
                stopped = True
            if self._install_root.exists():
                backup.mkdir(parents=True, exist_ok=True)
                for child in self._install_root.iterdir():
                    _atomic_move(child, backup / child.name)
            for child in extract_dir.iterdir():
                _atomic_move(child, self._install_root / child.name)
            staging = self._install_root.parent / f"{self._install_root.name}.engine-staging"
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if start_callback is not None:
                start_callback()
                started_again = True
        except Exception as exc:
            # Roll back: restore the backup, then bring the engine back.
            try:
                if self._install_root.exists():
                    shutil.rmtree(self._install_root, ignore_errors=True)
                if backup.exists():
                    backup.rename(self._install_root)
            except Exception as rex:
                logger.warning("rollback failed: %s", rex)
            if start_callback is not None and not started_again:
                try:
                    start_callback()
                except Exception as rex:
                    logger.warning("engine restart after rollback failed: %s", rex)
            yield {"phase": "error", "message": f"替换失败，已回滚: {exc}"}
            return
        if stopped and start_callback is None:
            logger.warning("engine stopped by update but no start_callback given")
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)

        self._local_cache = None
        logger.info("Engine updated to %s (asset %s)", tag, asset)
        yield {"phase": "complete", "tag": tag}


    @staticmethod
    def _verify_binary(server_bin: Path, tag: str) -> bool:
        """Executability check: run the new binary and confirm the build
        number matches the release tag. (A checksum proves integrity of
        the file; this proves it actually launches.)"""
        try:
            out = subprocess.run(
                [str(server_bin), "--version"],
                capture_output=True, text=True, timeout=10.0,
            )
        except Exception:
            return False
        got = parse_build_version(out.stdout or out.stderr)
        want = parse_tag_build(tag)
        return got is not None and (want is None or got == want)


def _find_llama_server(root: Path) -> Optional[Path]:
    """Locate llama-server[.exe] inside an extracted release."""
    exe = "llama-server.exe" if platform.system() == "Windows" else "llama-server"
    for p in root.rglob(exe):
        return p
    return None


def _safe_zip_extract(archive: Path, dest: Path) -> None:
    """Extract a zip guarding against path traversal."""
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            name = member.filename.replace("\\", "/")
            target = (dest / name).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise RuntimeError(f"zip entry escapes extract dir: {name}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
