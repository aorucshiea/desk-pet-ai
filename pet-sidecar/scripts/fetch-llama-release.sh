#!/usr/bin/env bash
# Download the official llama.cpp llama-server binary for this host.
#
# Output:
#   bin/<os>-<arch>/llama-server
#   bin/<os>-<arch>/*.dylib|*.so   (runtime libraries from the release)
#
# Honors:
#   LLAMA_CPP_RELEASE = b9371 by default
#   LLAMA_BACKEND     = auto | cpu | vulkan
#   TARGET_TRIPLE     = mac-arm64 | mac-x64 | linux-x64 | linux-arm64
#   LLAMA_OUT_DIR     = override output directory

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

cyan()  { printf "\033[36m%s\033[0m\n" "$*"; }
red()   { printf "\033[31m%s\033[0m\n" "$*" >&2; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }

TAG="${LLAMA_CPP_RELEASE:-b9371}"
BACKEND="${LLAMA_BACKEND:-auto}"

if [[ -n "${TARGET_TRIPLE:-}" ]]; then
  TARGET="$TARGET_TRIPLE"
else
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)  TARGET="mac-arm64" ;;
    Darwin-x86_64) TARGET="mac-x64" ;;
    Linux-x86_64)  TARGET="linux-x64" ;;
    Linux-aarch64) TARGET="linux-arm64" ;;
    *)
      red "Unsupported host: $(uname -s) $(uname -m). Windows uses fetch-llama-release.ps1."
      exit 1
      ;;
  esac
fi

case "$TARGET" in
  mac-arm64)   ASSET="llama-${TAG}-bin-macos-arm64.tar.gz" ;;
  mac-x64)     ASSET="llama-${TAG}-bin-macos-x64.tar.gz" ;;
  linux-x64)
    if [[ "$BACKEND" == "auto" || "$BACKEND" == "vulkan" ]]; then
      ASSET="llama-${TAG}-bin-ubuntu-vulkan-x64.tar.gz"
    else
      ASSET="llama-${TAG}-bin-ubuntu-x64.tar.gz"
    fi
    ;;
  linux-arm64)
    if [[ "$BACKEND" == "auto" || "$BACKEND" == "vulkan" ]]; then
      ASSET="llama-${TAG}-bin-ubuntu-vulkan-arm64.tar.gz"
    else
      ASSET="llama-${TAG}-bin-ubuntu-arm64.tar.gz"
    fi
    ;;
  *)
    red "Unsupported TARGET_TRIPLE=$TARGET"
    exit 1
    ;;
esac

OUT="${LLAMA_OUT_DIR:-$ROOT/bin/$TARGET}"
REL_URL="https://github.com/ggml-org/llama.cpp/releases/download/${TAG}/${ASSET}"
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# GitHub direct is unusable from mainland China (measured 0.01 MB/s on a 252 MB
# asset). Mirrors measured 1.1-1.5 MB/s, so try them first, official last.
# Override with: LLAMA_CPP_MIRRORS="https://a/https://github.com/...,https://b/..."
MIRRORS="$REL_URL"
for prefix in https://ghfast.top https://ghproxy.net https://gh-proxy.com; do
  MIRRORS="${prefix}/${REL_URL} ${MIRRORS}"
done
if [[ -n "${LLAMA_CPP_MIRRORS:-}" ]]; then
  MIRRORS="${LLAMA_CPP_MIRRORS//,/ }"
fi

cyan "==> Fetch llama.cpp ${TAG}: ${ASSET}"
mkdir -p "$OUT"
GOT=0
for url in $MIRRORS; do
  echo "  try: $url"
  rm -f "$TMP/$ASSET"
  if curl -sL --fail --noproxy '*' --max-time 1800 --retry 2 --retry-delay 2 \
       -o "$TMP/$ASSET" "$url"; then
    if [[ -s "$TMP/$ASSET" ]]; then
      green "  got it from: $url"
      GOT=1
      break
    fi
  fi
  echo "    failed, next source"
done
if [[ "$GOT" != "1" ]]; then
  red "all download sources failed for ${ASSET} (set LLAMA_CPP_MIRRORS to override)"
  exit 1
fi
mkdir -p "$TMP/extract"
tar -xzf "$TMP/$ASSET" -C "$TMP/extract"

SERVER="$(find "$TMP/extract" -type f -name llama-server | head -1 || true)"
if [[ -z "$SERVER" ]]; then
  red "llama-server not found in ${ASSET}"
  exit 1
fi

SERVER_DIR="$(dirname "$SERVER")"
RELEASE_ROOT="$SERVER_DIR"
while [[ "$(dirname "$RELEASE_ROOT")" != "$TMP/extract" && "$RELEASE_ROOT" != "$TMP/extract" ]]; do
  RELEASE_ROOT="$(dirname "$RELEASE_ROOT")"
done

cp -R "$RELEASE_ROOT"/. "$OUT"/
if [[ ! -f "$OUT/llama-server" ]]; then
  cp -R "$SERVER_DIR"/. "$OUT"/
fi
chmod +x "$OUT/llama-server" || true

green "==> OK -> $OUT/llama-server"
"$OUT/llama-server" --version
