#!/usr/bin/env bash
# Start/stop the optional Laya sidecar (macOS/Linux).
#
# Default action: start laya-serve on 127.0.0.1:8765, wait for /health, then
# send one warmup POST to /v1/systemone.  Exit 0 only when both succeeded.
#
#   ./start.sh
#   ./start.sh --status
#   ./start.sh --stop
#   ./start.sh --foreground
#
# For a smoke test against the spike cache:
#   ./start.sh --python ../../.tmp-laya/venv/bin/python \
#              --model ../../.tmp-laya/models/laya-multilingual

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ROOT/../.." && pwd)"
RUN_DIR="$ROOT/run"
PID_FILE="$RUN_DIR/laya-serve.pid"
PORT_FILE="$RUN_DIR/laya-serve.port"
OUT_LOG="$RUN_DIR/laya-serve.out.log"
ERR_LOG="$RUN_DIR/laya-serve.err.log"
MODEL_FILES=(
    "config.json"
    "rl_agent_config.json"
    "model.safetensors"
    "encoder/config.json"
    "tokenizer/tokenizer.json"
    "tokenizer/tokenizer_config.json"
)

PORT=8765
PORT_EXPLICIT=0
PYTHON_ARG="${LAYA_SIDECAR_PYTHON:-}"
MODEL_ARG="${LAYA_MODEL_PATH:-}"
TIMEOUT=240
ACTION="start"
FOREGROUND=0

usage() {
    cat <<'EOF'
Usage: ./start.sh [options]

  --port N          bind 127.0.0.1:N (default: 8765)
  --python PATH     venv Python (default: .venv, then the spike venv)
  --model PATH      laya-multilingual directory (default: models/, then .tmp-laya)
  --timeout N       seconds to wait for startup (default: 240)
  --status          print health and PID; exit 0 only when healthy
  --stop            stop the managed process
  --foreground      run laya-serve in this terminal (no PID file)
  -h, --help        this help
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
        --port)
            [[ $# -ge 2 ]] || die "--port needs a value"
            PORT="$2"; PORT_EXPLICIT=1; shift ;;
        --python)
            [[ $# -ge 2 ]] || die "--python needs a value"
            PYTHON_ARG="$2"; shift ;;
        --model)
            [[ $# -ge 2 ]] || die "--model needs a value"
            MODEL_ARG="$2"; shift ;;
        --timeout)
            [[ $# -ge 2 ]] || die "--timeout needs a value"
            TIMEOUT="$2"; shift ;;
        --stop) ACTION="stop" ;;
        --status) ACTION="status" ;;
        --foreground) FOREGROUND=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; die "unknown option: $1" ;;
    esac
    shift
done

[[ "$PORT" =~ ^[0-9]+$ ]] || die "--port must be an integer"
(( PORT >= 1 && PORT <= 65535 )) || die "--port must be 1-65535"
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] || die "--timeout must be an integer"
(( TIMEOUT >= 10 )) || die "--timeout must be at least 10 seconds"

read_pid() {
    if [[ ! -f "$PID_FILE" ]]; then
        echo 0
        return
    fi
    local value
    value="$(tr -d '[:space:]' < "$PID_FILE")"
    [[ "$value" =~ ^[0-9]+$ ]] || value=0
    echo "$value"
}

read_port() {
    local default_port="$1"
    if [[ ! -f "$PORT_FILE" ]]; then
        echo "$default_port"
        return
    fi
    local value
    value="$(tr -d '[:space:]' < "$PORT_FILE")"
    if [[ "$value" =~ ^[0-9]+$ ]] && (( value >= 1 && value <= 65535 )); then
        echo "$value"
    else
        echo "$default_port"
    fi
}

find_venv_python() {
    if [[ -x "$ROOT/.venv/bin/python" ]]; then
        printf '%s\n' "$ROOT/.venv/bin/python"
        return 0
    fi
    if [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
        printf '%s\n' "$ROOT/.venv/Scripts/python.exe"
        return 0
    fi
    if [[ -x "$REPO_ROOT/.tmp-laya/venv/bin/python" ]]; then
        printf '%s\n' "$REPO_ROOT/.tmp-laya/venv/bin/python"
        return 0
    fi
    if [[ -x "$REPO_ROOT/.tmp-laya/venv/Scripts/python.exe" ]]; then
        printf '%s\n' "$REPO_ROOT/.tmp-laya/venv/Scripts/python.exe"
        return 0
    fi
    return 1
}

python_imports() {
    "$1" -c 'import laya, laya.serve, torch, fastapi, uvicorn' >/dev/null 2>&1
}

resolve_python() {
    if [[ -n "$PYTHON_ARG" ]]; then
        [[ -x "$PYTHON_ARG" ]] || die "Python not found or not executable: $PYTHON_ARG"
        python_imports "$PYTHON_ARG" || die "Python cannot import laya, torch, fastapi, uvicorn: $PYTHON_ARG"
        printf '%s\n' "$PYTHON_ARG"
        return 0
    fi

    local candidate
    for candidate in "$ROOT/.venv/bin/python" "$ROOT/.venv/Scripts/python.exe"; do
        if [[ -x "$candidate" ]]; then
            if python_imports "$candidate"; then
                printf '%s\n' "$candidate"
                return 0
            fi
            warn "ignoring Python without laya[serve]/torch: $candidate"
        fi
    done
    for candidate in "$REPO_ROOT/.tmp-laya/venv/bin/python" "$REPO_ROOT/.tmp-laya/venv/Scripts/python.exe"; do
        if [[ -x "$candidate" ]] && python_imports "$candidate"; then
            warn "sidecar venv missing; using spike venv: $candidate"
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    die "No usable sidecar Python. Run ./bootstrap.sh, or pass --python."
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
    for candidate in "$ROOT/models/laya-multilingual" "$REPO_ROOT/.tmp-laya/models/laya-multilingual"; do
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

health_ok() {
    local port="$1"
    curl -fsS --max-time 3 "http://127.0.0.1:$port/health" 2>/dev/null \
        | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'
}

port_in_use() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
        return 1
    fi
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$port$" && return 0
        return 1
    fi
    if command -v netstat >/dev/null 2>&1; then
        netstat -an 2>/dev/null | grep -qE "[:.]$port[[:space:]].*LISTEN" && return 0
        return 1
    fi
    health_ok "$port"
}

show_logs() {
    local file
    for file in "$OUT_LOG" "$ERR_LOG"; do
        if [[ -f "$file" ]]; then
            printf -- '--- %s (last 40 lines) ---\n' "$file" >&2
            tail -n 40 "$file" >&2 || true
        fi
    done
}

stop_pid() {
    local server_pid="$1"
    local effective_port="$2"
    local command_line
    command_line="$(ps -p "$server_pid" -o args= 2>/dev/null || true)"
    if [[ -n "$command_line" && "$command_line" != *"laya.serve"* ]]; then
        die "PID $server_pid is not laya-serve; refusing to stop it: $command_line"
    fi
    kill "$server_pid" 2>/dev/null || true
    local deadline=$((SECONDS + 15))
    while (( SECONDS < deadline )); do
        if ! kill -0 "$server_pid" 2>/dev/null; then
            break
        fi
        sleep 0.2
    done
    if kill -0 "$server_pid" 2>/dev/null; then
        warn "PID $server_pid did not exit after SIGTERM; sending SIGKILL"
        kill -9 "$server_pid" 2>/dev/null || true
        sleep 0.5
    fi
    rm -f "$PID_FILE" "$PORT_FILE"
    if kill -0 "$server_pid" 2>/dev/null; then
        die "PID $server_pid is still alive"
    fi
    if port_in_use "$effective_port"; then
        die "port $effective_port is still listening after stop"
    fi
}

if [[ "$ACTION" == "stop" ]]; then
    mkdir -p "$RUN_DIR"
    SERVER_PID="$(read_pid)"
    EFFECTIVE_PORT="$(read_port "$PORT")"
    if [[ "$SERVER_PID" -le 0 ]]; then
        if port_in_use "$EFFECTIVE_PORT"; then
            die "port $EFFECTIVE_PORT is in use but no PID file; refusing to kill an unmanaged process"
        fi
        printf 'Laya sidecar is not running.\n'
        exit 0
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        rm -f "$PID_FILE" "$PORT_FILE"
        if port_in_use "$EFFECTIVE_PORT"; then
            warn "stale PID removed, but port $EFFECTIVE_PORT is still in use"
        fi
        printf 'Laya sidecar is not running (stale PID removed).\n'
        exit 0
    fi
    step "Stopping laya-serve (PID $SERVER_PID, port $EFFECTIVE_PORT)"
    stop_pid "$SERVER_PID" "$EFFECTIVE_PORT"
    ok "stopped; port $EFFECTIVE_PORT is free"
    exit 0
fi

if [[ "$ACTION" == "status" ]]; then
    mkdir -p "$RUN_DIR"
    SERVER_PID="$(read_pid)"
    EFFECTIVE_PORT="$(read_port "$PORT")"
    ALIVE=0
    if [[ "$SERVER_PID" -gt 0 ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        ALIVE=1
    fi
    if health_ok "$EFFECTIVE_PORT"; then
        if [[ "$ALIVE" == "1" ]]; then
            ok "running: PID $SERVER_PID, http://127.0.0.1:$EFFECTIVE_PORT"
        else
            warn "health is OK but no live managed PID; the server was started outside start.sh."
        fi
        curl -fsS --max-time 3 "http://127.0.0.1:$EFFECTIVE_PORT/health"
        printf '\n'
        exit 0
    fi
    if [[ "$ALIVE" == "1" ]]; then
        printf '[error] PID %s is alive but /health is not OK on port %s.\n' "$SERVER_PID" "$EFFECTIVE_PORT" >&2
        show_logs
        exit 2
    fi
    printf 'Laya sidecar is not running.\n'
    exit 1
fi

MODEL_PATH="$(resolve_model_path "$MODEL_ARG" || true)"
if [[ -n "$MODEL_ARG" && -z "$MODEL_PATH" ]]; then
    die "Model path is not a complete laya-multilingual checkpoint: $MODEL_ARG"
fi
[[ -n "$MODEL_PATH" ]] || die "No complete laya-multilingual checkpoint found. Run ./bootstrap.sh first, or pass --model."

PYTHON_BIN="$(resolve_python)"
command -v curl >/dev/null 2>&1 || die "curl is required for health checks"

# Reuse the port recorded by the running process unless the caller asked for
# a specific one.
SERVER_PID="$(read_pid)"
EFFECTIVE_PORT="$PORT"
if [[ "$SERVER_PID" -gt 0 ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    MANAGED_PORT="$(read_port "$PORT")"
    if [[ "$PORT_EXPLICIT" == "1" && "$MANAGED_PORT" != "$PORT" ]]; then
        die "laya-serve is already running on port $MANAGED_PORT; run --stop first or omit --port."
    fi
    EFFECTIVE_PORT="$MANAGED_PORT"
    if health_ok "$EFFECTIVE_PORT"; then
        ok "already running: PID $SERVER_PID, http://127.0.0.1:$EFFECTIVE_PORT"
        exit 0
    fi
    die "PID $SERVER_PID is alive but /health is not ready on port $EFFECTIVE_PORT. Run --stop or inspect $ERR_LOG."
fi

if [[ "$SERVER_PID" -gt 0 ]]; then
    warn "removing stale PID file: $PID_FILE"
    rm -f "$PID_FILE" "$PORT_FILE"
    EFFECTIVE_PORT="$PORT"
fi

if port_in_use "$EFFECTIVE_PORT"; then
    die "port $EFFECTIVE_PORT is already in use; refusing to start a duplicate"
fi

export LAYA_HOST="127.0.0.1"
export LAYA_PORT="$EFFECTIVE_PORT"
export LAYA_DEVICE="cpu"
export LAYA_EXTRA_MODELS="$("$PYTHON_BIN" -c 'import json,sys; print(json.dumps({"zh-tier": sys.argv[1]}, separators=(",",":")))' "$MODEL_PATH")"
export LAYA_MODELS="zh-tier"
export LAYA_DEFAULT_MODEL="zh-tier"
export LAYA_PRELOAD="1"
export LAYA_MAX_LOADED="1"
export LAYA_IDLE_UNLOAD_SECONDS="0"
export LAYA_LOG_LEVEL="info"
export LAYA_THREADS="${LAYA_THREADS:-4}"
export PYTHONUTF8="1"
export PYTHONIOENCODING="utf-8"
export HF_HUB_OFFLINE="1"
unset LAYA_API_KEY

if [[ "$FOREGROUND" == "1" ]]; then
    step "Running in foreground: $PYTHON_BIN -m laya.serve"
    exec "$PYTHON_BIN" -m laya.serve
fi

mkdir -p "$RUN_DIR"
for file in "$OUT_LOG" "$ERR_LOG"; do
    if [[ -f "$file" ]]; then
        mv -f "$file" "$file.prev"
    fi
done

step "Starting laya-serve on 127.0.0.1:$EFFECTIVE_PORT (model: $MODEL_PATH)"
nohup "$PYTHON_BIN" -m laya.serve >"$OUT_LOG" 2>"$ERR_LOG" &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"
echo "$EFFECTIVE_PORT" > "$PORT_FILE"
printf '    PID %s, log: %s\n' "$SERVER_PID" "$ERR_LOG"

DEADLINE=$((SECONDS + TIMEOUT))
READY=0
while (( SECONDS < DEADLINE )); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        show_logs
        rm -f "$PID_FILE" "$PORT_FILE"
        die "laya-serve exited during startup"
    fi
    if health_ok "$EFFECTIVE_PORT"; then
        READY=1
        break
    fi
    sleep 0.5
done

if [[ "$READY" != "1" ]]; then
    show_logs
    kill "$SERVER_PID" 2>/dev/null || true
    rm -f "$PID_FILE" "$PORT_FILE"
    die "server did not become healthy within ${TIMEOUT}s"
fi
ok "health OK"

WARMUP_BODY='{"model":"zh-tier","state":"ping","questions":{"thinking":{"type":"choice","instructions":"How much thinking budget does this message need?","criteria":{"fast":"greeting or trivial","normal":"simple chat or a short factual answer","deep":"code, error analysis, or multi-step work","max":"architecture or an explicitly long design task"}}}}'
WARMUP_FILE="$RUN_DIR/warmup.json"
WARMUP_RESULT="$(curl -sS --max-time 120 -o "$WARMUP_FILE" -w '%{http_code} %{time_total}' \
    -X POST "http://127.0.0.1:$EFFECTIVE_PORT/v1/systemone" \
    -H 'Content-Type: application/json; charset=utf-8' \
    --data-binary "$WARMUP_BODY" 2>/dev/null || true)"
WARMUP_CODE="${WARMUP_RESULT%% *}"
WARMUP_SECONDS="${WARMUP_RESULT##* }"

if [[ "$WARMUP_CODE" != "200" ]]; then
    show_logs
    kill "$SERVER_PID" 2>/dev/null || true
    rm -f "$PID_FILE" "$PORT_FILE"
    die "warmup /v1/systemone returned HTTP ${WARMUP_CODE:-none} (expected 200)"
fi
if ! grep -q '"choice"' "$WARMUP_FILE"; then
    show_logs
    kill "$SERVER_PID" 2>/dev/null || true
    rm -f "$PID_FILE" "$PORT_FILE"
    die "warmup response has no choice: $(cat "$WARMUP_FILE")"
fi

CHOICE="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1]))["answers"]["thinking"]["choice"])' "$WARMUP_FILE")"
WARMUP_MS="$("$PYTHON_BIN" -c 'import sys; print(round(float(sys.argv[1]) * 1000))' "$WARMUP_SECONDS")"
ok "ready: PID $SERVER_PID, choice=$CHOICE, warmup=${WARMUP_MS} ms"
printf '    health : http://127.0.0.1:%s/health\n' "$EFFECTIVE_PORT"
printf '    decide : POST http://127.0.0.1:%s/v1/systemone  (model=zh-tier)\n' "$EFFECTIVE_PORT"
printf '    log    : %s / %s\n' "$OUT_LOG" "$ERR_LOG"
