#!/usr/bin/env python3
"""Provision the optional laya-multilingual checkpoint for the Laya sidecar.

This helper never needs torch.  It only downloads the tokenizer/config/weights
that ``laya-serve`` later loads, or verifies a directory that is already there.
ONNX export lives in ``export_onnx.py`` because export does require torch.

Usage:
    python provision.py download --source auto --dest models/laya-multilingual
    python provision.py verify   --model models/laya-multilingual
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MODEL_REPO = "convaiinnovations/laya-multilingual"
MODEL_FILES = [
    "config.json",
    "rl_agent_config.json",
    "model.safetensors",
    "encoder/config.json",
    "tokenizer/tokenizer.json",
    "tokenizer/tokenizer_config.json",
]
# Guard against silently keeping a truncated response as a valid checkpoint.
MIN_SIZES = {
    "model.safetensors": 600_000_000,
    "tokenizer/tokenizer.json": 30_000_000,
}
MODELSCOPE_BASE = "https://modelscope.cn/models/%s/resolve/master" % MODEL_REPO
USER_AGENT = "deskpet-laya-sidecar/1.0"


def _human(n: int) -> str:
    if n >= 1_000_000_000:
        return "%.2f GB" % (n / 1_000_000_000)
    if n >= 1_000_000:
        return "%.1f MB" % (n / 1_000_000)
    if n >= 1_000:
        return "%.1f KB" % (n / 1_000)
    return "%d B" % n


def check_model_dir(path: Path) -> tuple[bool, str]:
    """Return (ok, reason) for a local laya checkpoint directory."""
    if not path.exists():
        return False, "directory does not exist: %s" % path
    if not path.is_dir():
        return False, "not a directory: %s" % path

    missing = [rel for rel in MODEL_FILES if not (path / rel).is_file()]
    if missing:
        return False, "missing files: %s" % ", ".join(missing)

    for rel, minimum in MIN_SIZES.items():
        size = (path / rel).stat().st_size
        if size < minimum:
            return False, "%s is %s, expected at least %s (probably truncated)" % (
                rel,
                _human(size),
                _human(minimum),
            )

    try:
        with (path / "rl_agent_config.json").open("r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:  # noqa: BLE001 - report the real parse error
        return False, "rl_agent_config.json is not valid JSON: %s" % exc
    if not isinstance(cfg, dict) or "encoder" not in cfg:
        return False, "rl_agent_config.json does not look like a Laya checkpoint"

    total = sum((path / rel).stat().st_size for rel in MODEL_FILES)
    return True, "%s (%s)" % (path, _human(total))


def _download_once(url: str, out: Path, retries: int = 3) -> int:
    """Stream URL to ``out``, resuming ``out.part`` when the server supports it."""
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")

    last_error = None
    for attempt in range(1, retries + 1):
        offset = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if offset:
            headers["Range"] = "bytes=%d-" % offset
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                status = getattr(response, "status", 200)
                if offset and status == 206:
                    mode = "ab"
                    start = offset
                else:
                    # The server ignored Range, or this is the first attempt.
                    mode = "wb"
                    start = 0
                content_type = (response.headers.get("Content-Type") or "").lower()
                if "text/html" in content_type and not out.name.endswith(".json"):
                    raise IOError("server returned HTML instead of %s" % out.name)
                expected = int(response.headers.get("Content-Length") or 0)
                with part.open(mode) as fh:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
            written = part.stat().st_size
            if expected and written < start + expected:
                raise IOError(
                    "short read for %s: got %s of %s bytes"
                    % (out.name, _human(written), _human(start + expected))
                )
            os.replace(part, out)
            return out.stat().st_size
        except Exception as exc:  # noqa: BLE001 - retry network failures
            last_error = exc
            if attempt == retries:
                break
            delay = 2 ** attempt
            print("  retry %d/%d for %s in %ds: %s" % (attempt, retries, out.name, delay, exc))
            time.sleep(delay)
    raise RuntimeError("download failed for %s: %s" % (out.name, last_error))


def download_hf(dest: Path) -> None:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "huggingface_hub is not installed; run bootstrap.ps1/bootstrap.sh first"
        ) from exc

    endpoint = os.environ.get("HF_ENDPOINT") or "https://huggingface.co"
    print("source: HuggingFace (%s) -> %s" % (endpoint, dest))
    snapshot_download(
        repo_id=MODEL_REPO,
        local_dir=str(dest),
        allow_patterns=MODEL_FILES,
    )


def download_modelscope(dest: Path) -> None:
    print("source: ModelScope (%s) -> %s" % (MODEL_REPO, dest))
    for rel in MODEL_FILES:
        out = dest / rel
        minimum = MIN_SIZES.get(rel, 1)
        if out.is_file() and out.stat().st_size >= minimum:
            print("  skip %s (%s)" % (rel, _human(out.stat().st_size)))
            continue
        url = "%s/%s" % (MODELSCOPE_BASE, rel)
        print("  fetch %s" % rel)
        size = _download_once(url, out)
        if size < minimum:
            raise RuntimeError(
                "%s is %s, expected at least %s; refusing a truncated checkpoint"
                % (rel, _human(size), _human(minimum))
            )
        print("  ok    %s (%s)" % (rel, _human(size)))


def cmd_download(args: argparse.Namespace) -> int:
    dest = Path(args.dest).expanduser().resolve()
    ok, reason = check_model_dir(dest)
    if ok:
        print("model already present: %s" % reason)
        print("nothing downloaded; use a different --dest to re-provision")
        return 0

    if dest.exists() and any(dest.iterdir()):
        print("destination is incomplete (%s); resuming/repairing" % reason)

    source = args.source
    errors = []
    if source in ("auto", "hf"):
        try:
            download_hf(dest)
            ok, reason = check_model_dir(dest)
            if ok:
                print("model ready: %s" % reason)
                return 0
            errors.append("HuggingFace finished but verification failed: %s" % reason)
        except Exception as exc:  # noqa: BLE001 - auto falls back to ModelScope
            errors.append("HuggingFace failed: %s" % exc)
            if source == "hf":
                print("ERROR: %s" % errors[-1], file=sys.stderr)
                return 1
            print("HuggingFace failed, falling back to ModelScope: %s" % exc)

    if source in ("auto", "modelscope"):
        try:
            download_modelscope(dest)
            ok, reason = check_model_dir(dest)
            if ok:
                print("model ready: %s" % reason)
                return 0
            errors.append("ModelScope finished but verification failed: %s" % reason)
        except Exception as exc:  # noqa: BLE001 - report the real failure
            errors.append("ModelScope failed: %s" % exc)

    print("ERROR: could not provision laya-multilingual.", file=sys.stderr)
    for error in errors:
        print("  - %s" % error, file=sys.stderr)
    print(
        "Manual options: set HF_ENDPOINT=https://hf-mirror.com and retry, "
        "or download the files listed in provision.py from ModelScope.",
        file=sys.stderr,
    )
    return 1


def cmd_verify(args: argparse.Namespace) -> int:
    ok, reason = check_model_dir(Path(args.model).expanduser().resolve())
    print(("OK: " if ok else "ERROR: ") + reason)
    if ok and args.onnx:
        onnx_path = Path(args.onnx).expanduser().resolve()
        if not onnx_path.is_file():
            print("ERROR: ONNX file not found: %s" % onnx_path, file=sys.stderr)
            return 1
        size = onnx_path.stat().st_size
        if size < 1_000_000_000:
            print(
                "ERROR: ONNX file is %s, expected a full fp32 export (>1 GB)" % _human(size),
                file=sys.stderr,
            )
            return 1
        print("OK: ONNX %s (%s)" % (onnx_path, _human(size)))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    dl = sub.add_parser("download", help="download the checkpoint files")
    dl.add_argument("--source", choices=("auto", "hf", "modelscope"), default="auto")
    dl.add_argument("--dest", required=True, help="local checkpoint directory")
    dl.set_defaults(func=cmd_download)

    verify = sub.add_parser("verify", help="validate a checkpoint directory")
    verify.add_argument("--model", required=True)
    verify.add_argument("--onnx", default=None)
    verify.set_defaults(func=cmd_verify)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
