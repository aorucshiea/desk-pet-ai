#!/usr/bin/env bash
# Optional Laya sidecar bootstrap (macOS/Linux; Git Bash mostly works but
# start.ps1 is the Windows path).
#
# Creates .venv, installs laya[onnx,serve]==0.4.15, provisions laya-multilingual, and
# optionally exports ONNX.  Nothing large is bundled.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ROOT/../.." && pwd)"
VENV_DIR="$ROOT/.venv"
MODEL_DIR="$ROOT/models/laya-multilingual"
MODEL_FILES=(
    "config.json"
    "rl_agent_config.json"
    "model.safetensors"
    "encoder/config.json"
    "tokenizer/tokenizer.json"
    "tokenizer/tokenizer_config.json"
)

SOURCE="auto"
CHECK=0
EXPORT_ONNX=0
PYTHON_ARG="${LAYA_PYTHON:-}"
MODEL_ARG="${LAYA_MODEL_PATH:-}"
PIP_INDEX_ARG="${LAYA_PIP_INDEX_URL:-}"
OFFICIAL_PYPI=0

usage() {
    cat <<'EOF'
Usage: ./bootstrap.sh [options]

  --check                 report Python/venv/model/ONNX status, change nothing
  --export-onnx           export ONNX after install (one-off, ~1.3 GB)
  --source auto|hf|modelscope
                          checkpoint download source (default: auto; HF then ModelScope)
  --python PATH           Python 3.10-3.12 executable
  --model PATH            reuse an existing laya-multilingual directory
  --pip-index-url URL     pip index (default: Tsinghua mirror)
  --official-pypi         use https://pypi.org/simple instead of the mirror
  -h, --help              this help

Environment: LAYA_PYTHON, LAYA_MODEL_PATH, LAYA_PIP_INDEX_URL,
             HF_ENDPOINT (e.g. https://hf-mirror.com).
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

warn() {
    printf '[warn] %s\n' "$*" >&2
}

step() {
    printf '==> %s\n' "$*"
}

ok() {
    printf '[ok] %s\n' "$*"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) CHECK=1 ;;
        --export-onnx) EXPORT_ONNX=1 ;;
        --source)
            [[ $# -ge 2 ]] || die "--source needs a value"
            SOURCE="$2"; shift ;;
        --python)
            [[ $# -ge 2 ]] || die "--python needs a value"
            PYTHON_ARG="$2"; shift ;;
        --model)
            [[ $# -ge 2 ]] || die "--model needs a value"
            MODEL_ARG="$2"; shift ;;
        --pip-index-url)
            [[ $# -ge 2 ]] || die "--pip-index-url needs a value"
            PIP_INDEX_ARG="$2"; shift ;;
        --official-pypi) OFFICIAL_PYPI=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; die "unknown option: $1" ;;
    esac
    shift
done

case "$SOURCE" in
    auto|hf|modelscope) ;;
    *) die "--source must be auto, hf, or modelscope (got: $SOURCE)" ;;
esac

python_version() {
    "$1" -c 'import sys,struct; print("%d.%d %d" % (sys.version_info[0], sys.version_info[1], struct.calcsize("P") * 8))' 2>/dev/null || return 1
}

version_ok() {
    local version_line="$1"
    local version bits
    read -r version bits <<< "$version_line"
    [[ "$bits" == "64" ]] || return 1
    case "$version" in
        3.10|3.11|3.12) return 0 ;;
        *) return 1 ;;
    esac
}

find_python() {
    if [[ -n "$PYTHON_ARG" ]]; then
        local line
        line="$(python_version "$PYTHON_ARG" || true)"
        if [[ -n "$line" ]] && version_ok "$line"; then
            printf '%s\n' "$PYTHON_ARG"
            return 0
        fi
        die "LAYA_PYTHON/--python must be a 64-bit Python 3.10-3.12: $PYTHON_ARG"
    fi

    local candidate line
    for candidate in python3.12 python3.11 python3.10 python3 python; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        line="$(python_version "$candidate" || true)"
        if [[ -n "$line" ]] && version_ok "$line"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    if command -v uv >/dev/null 2>&1; then
        local version resolved
        for version in 3.12 3.11 3.10; do
            resolved="$(uv python find "$version" 2>/dev/null | tail -n 1 | tr -d '\r' || true)"
            if [[ -n "$resolved" ]]; then
                line="$(python_version "$resolved" || true)"
                if [[ -n "$line" ]] && version_ok "$line"; then
                    printf '%s\n' "$resolved"
                    return 0
                fi
            fi
        done
    fi
    die "No 64-bit Python 3.10-3.12 found. Install Python 3.12 or pass --python."
}

find_venv_python() {
    if [[ -x "$VENV_DIR/bin/python" ]]; then
        printf '%s\n' "$VENV_DIR/bin/python"
        return 0
    fi
    if [[ -x "$VENV_DIR/Scripts/python.exe" ]]; then
        printf '%s\n' "$VENV_DIR/Scripts/python.exe"
        return 0
    fi
    return 1
}

model_is_complete() {
    local path="$1"
    [[ -d "$path" ]] || return 1
    local rel
    for rel in "${MODEL_FILES[@]}"; do
        [[ -f "$path/$rel" ]] || return 1
    done
    [[ "$(wc -c < "$path/model.safetensors" | tr -d '[:space:]')" -ge 600000000 ]] || return 1
    [[ "$(wc -c < "$path/tokenizer/tokenizer.json" | tr -d '[:space:]')" -ge 30000000 ]] || return 1
    grep -q '"encoder"' "$path/rl_agent_config.json" 2>/dev/null || return 1
    return 0
}

resolve_model_path() {
    local explicit="${1:-}"
    if [[ -n "$explicit" ]]; then
        if model_is_complete "$explicit"; then
            (cd "$explicit" && pwd)
            return 0
        fi
        printf 'ERROR: Model path is not a complete laya-multilingual checkpoint: %s\n' "$explicit" >&2
        return 1
    fi
    local candidate
    for candidate in "$MODEL_DIR" "$REPO_ROOT/.tmp-laya/models/laya-multilingual"; do
        if model_is_complete "$candidate"; then
            (cd "$candidate" && pwd)
            return 0
        fi
        if [[ -e "$candidate" ]]; then
            warn "ignoring incomplete model directory: $candidate"
        fi
    done
    return 1
}

find_onnx() {
    local candidate size
    for candidate in "$ROOT/models/laya-multilingual.onnx" "$REPO_ROOT/.tmp-laya/laya-multilingual.onnx"; do
        if [[ -f "$candidate" ]]; then
            size="$(wc -c < "$candidate" | tr -d '[:space:]')"
            if [[ "$size" -gt 1000000000 ]]; then
                (cd "$(dirname "$candidate")" && printf '%s/%s\n' "$(pwd)" "$(basename "$candidate")")
                return 0
            fi
        fi
    done
    return 1
}

index_url() {
    if [[ -n "$PIP_INDEX_ARG" ]]; then
        printf '%s\n' "$PIP_INDEX_ARG"
    elif [[ "$OFFICIAL_PYPI" == "1" ]]; then
        printf '%s\n' "https://pypi.org/simple"
    elif [[ -n "${PIP_INDEX_URL:-}" ]]; then
        printf '%s\n' "$PIP_INDEX_URL"
    else
        printf '%s\n' "https://pypi.tuna.tsinghua.edu.cn/simple"
    fi
}

PYTHON_BIN="${PYTHON_ARG:-}"
VENV_PY=""
MODEL_PATH=""
if [[ "$CHECK" != "1" ]]; then
    step "Laya sidecar: $ROOT"
    MODEL_PATH="$(resolve_model_path "$MODEL_ARG" || true)"
    if [[ -n "$MODEL_ARG" && -z "$MODEL_PATH" ]]; then
        die "Model path is not a complete laya-multilingual checkpoint: $MODEL_ARG"
    fi
fi

if [[ "$CHECK" == "1" ]]; then
    READY=1
    VENV_PY="$(find_venv_python || true)"
    if [[ -n "$VENV_PY" ]]; then
        ok "venv: $VENV_DIR ($("$VENV_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "unusable"))"
        if "$VENV_PY" -c 'import laya, torch, fastapi, uvicorn' >/dev/null 2>&1; then
            ok "venv packages: laya/torch/fastapi/uvicorn"
        else
            warn "venv exists but lacks laya/torch/fastapi/uvicorn; rerun bootstrap"
            READY=0
        fi
    else
        warn "venv missing: $VENV_DIR"
        READY=0
    fi
    MODEL_PATH="$(resolve_model_path "$MODEL_ARG" || true)"
    if [[ -n "$MODEL_PATH" ]]; then
        ok "model: $MODEL_PATH"
    else
        warn "model missing: run ./bootstrap.sh"
        READY=0
    fi
    ONNX_PATH="$(find_onnx || true)"
    if [[ -n "$ONNX_PATH" ]]; then
        ok "onnx: $ONNX_PATH (optional; not used by laya-serve)"
    else
        printf '[--] onnx: not exported (optional; --export-onnx creates it)\n'
    fi
    [[ "$READY" == "1" ]] && exit 0
    exit 1
fi

if [[ -z "$VENV_PY" ]]; then
    VENV_PY="$(find_venv_python || true)"
fi
if [[ -z "$VENV_PY" ]]; then
    PYTHON_BIN="$(find_python)"
    PYTHON_LINE="$(python_version "$PYTHON_BIN")"
    ok "Python ${PYTHON_LINE} : $PYTHON_BIN"
    step "Creating venv: $VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    VENV_PY="$(find_venv_python || true)"
    [[ -n "$VENV_PY" ]] || die "python -m venv failed"
    ok "venv created"
else
    ok "venv already exists: $VENV_DIR ($("$VENV_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "unusable"))"
fi

INDEX="$(index_url)"
step "Installing laya[onnx,serve]==0.4.15 (index: $INDEX)"
printf '    torch is a hard dependency of laya; this is the large step.\n'
"$VENV_PY" -m pip install --disable-pip-version-check --timeout 60 --retries 5 \
    --index-url "$INDEX" 'laya[onnx,serve]==0.4.15' || die "pip install laya[onnx,serve]==0.4.15 failed"

step "Verifying imports"
"$VENV_PY" -c "import laya, torch, onnxruntime, fastapi, uvicorn; print('laya='+laya.__version__+' torch='+torch.__version__+' ort='+onnxruntime.__version__)" \
    || die "installed packages failed to import"
ok "imports verified"

if [[ -z "$MODEL_PATH" ]]; then
    step "Downloading laya-multilingual (source: $SOURCE) -> $MODEL_DIR"
    "$VENV_PY" "$ROOT/provision.py" download --source "$SOURCE" --dest "$MODEL_DIR" \
        || die "model download failed (partial files kept for resume)"
    model_is_complete "$MODEL_DIR" || die "downloaded model failed validation"
    MODEL_PATH="$(cd "$MODEL_DIR" && pwd)"
else
    ok "reusing model (no copy): $MODEL_PATH"
    case "$MODEL_PATH" in
        *".tmp-laya"*) warn "model is owned by the spike cache; delete .tmp-laya and rerun bootstrap to install into the sidecar" ;;
    esac
fi
ok "model ready: $MODEL_PATH"

if [[ "$EXPORT_ONNX" == "1" ]]; then
    EXISTING_ONNX="$(find_onnx || true)"
    if [[ -n "$EXISTING_ONNX" ]]; then
        ok "ONNX already exists: $EXISTING_ONNX (not copied)"
    else
        step "Exporting ONNX -> $ROOT/models/laya-multilingual.onnx"
        "$VENV_PY" "$ROOT/export_onnx.py" --model "$MODEL_PATH" --output "$ROOT/models/laya-multilingual.onnx" \
            || die "ONNX export failed"
        if [[ ! -f "$ROOT/models/laya-multilingual.onnx" ]] || [[ "$(wc -c < "$ROOT/models/laya-multilingual.onnx" | tr -d '[:space:]')" -lt 1000000000 ]]; then
            die "ONNX export did not produce a full file: $ROOT/models/laya-multilingual.onnx"
        fi
        ok "ONNX exported"
        warn "laya-serve 0.4.15 does not serve ONNX; start.sh still uses torch."
    fi
fi

printf '\n'
ok "Laya sidecar is ready."
printf '  start : %s/start.sh\n' "$ROOT"
printf '  status: %s/start.sh --status\n' "$ROOT"
printf '  stop  : %s/start.sh --stop\n' "$ROOT"
printf '  health: http://127.0.0.1:8765/health\n'
printf '  model : zh-tier (local, 127.0.0.1 only)\n'
