#!/usr/bin/env python3
"""Export laya-multilingual to ONNX (fp32) without downloading anything.

This mirrors the spike's exporter and is intentionally separate from
``laya-serve``: the shipped server only knows the torch ``Router``.  The ONNX
file is an optional artifact for a future ONNX runtime/server; exporting it is
not required for the torch-based sidecar to start.

Usage:
    python export_onnx.py --model models/laya-multilingual \
        --output models/laya-multilingual.onnx
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from provision import check_model_dir

# The exported graph takes exactly the five inputs ONNXAgent._collate_items_numpy
# produces.  K is kept at 2 because the traced topk(2) head needs two marker
# columns even when a request has one option.
BATCH = 2
SEQ = 32
MARKERS = 2


def export(model_dir: Path, output: Path, force: bool) -> int:
    ok, reason = check_model_dir(model_dir)
    if not ok:
        print("ERROR: invalid model directory: %s" % reason, file=sys.stderr)
        return 1

    if output.exists() and not force:
        existing_size = output.stat().st_size
        if existing_size >= 1_000_000_000:
            print("ONNX already exists: %s (%.2f GB)" % (output, existing_size / 1e9))
            print("not re-exporting; pass --force to replace it")
            return 0
        print("existing ONNX is only %.2f GB and looks incomplete; replacing it"
              % (existing_size / 1e9))

    try:
        import torch
        import laya
    except Exception as exc:  # noqa: BLE001 - torch/laya are required for export only
        print("ERROR: ONNX export needs torch and laya: %s" % exc, file=sys.stderr)
        print("Run bootstrap.ps1/bootstrap.sh first.", file=sys.stderr)
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep a .onnx suffix so torch.onnx.export treats the temporary file as a
    # normal graph; only rename after the size check succeeds.
    tmp = output.with_name(output.stem + ".part" + output.suffix)
    if tmp.exists():
        tmp.unlink()

    print("loading %s on CPU ..." % model_dir)
    agent = laya.load(str(model_dir), device="cpu")
    model = agent.model.eval()
    # Eager attention is the portable path; SDPA does not trace cleanly.
    try:
        model.encoder.config._attn_implementation = "eager"
    except Exception as exc:  # noqa: BLE001 - export can still try the default
        print("warning: could not switch attention to eager: %s" % exc, file=sys.stderr)

    dummy = (
        torch.randint(100, 2000, (BATCH, SEQ), dtype=torch.long),
        torch.ones((BATCH, SEQ), dtype=torch.long),
        torch.zeros((BATCH, MARKERS), dtype=torch.long),
        torch.ones((BATCH, MARKERS), dtype=torch.bool),
        torch.zeros(BATCH, dtype=torch.long),
    )

    started = time.perf_counter()
    try:
        torch.onnx.export(
            model,
            dummy,
            str(tmp),
            input_names=["input_ids", "attention_mask", "marker_pos", "marker_mask", "qtype"],
            output_names=["logits", "act_logits"],
            dynamic_axes={
                "input_ids": {0: "batch", 1: "seq"},
                "attention_mask": {0: "batch", 1: "seq"},
                "marker_pos": {0: "batch", 1: "markers"},
                "marker_mask": {0: "batch", 1: "markers"},
                "qtype": {0: "batch"},
                "logits": {0: "batch", 1: "markers"},
                "act_logits": {0: "batch"},
            },
            opset_version=17,
            dynamo=False,
            do_constant_folding=True,
        )
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise

    if not tmp.is_file() or tmp.stat().st_size < 1_000_000_000:
        if tmp.exists():
            tmp.unlink()
        print("ERROR: export produced no full fp32 graph", file=sys.stderr)
        return 1

    os.replace(tmp, output)
    elapsed = time.perf_counter() - started
    print(
        "exported %s in %.1fs (%.2f GB)"
        % (output, elapsed, output.stat().st_size / 1e9)
    )
    print("note: laya-serve 0.4.15 does not serve ONNX; this file is for a future runner")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="local laya-multilingual directory")
    parser.add_argument("--output", required=True, help="destination .onnx file")
    parser.add_argument("--force", action="store_true", help="replace an existing export")
    args = parser.parse_args()

    model_dir = Path(args.model).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    return export(model_dir, output, args.force)


if __name__ == "__main__":
    sys.exit(main())
