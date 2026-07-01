#!/usr/bin/env bash
# Ensure the top-level llama.cpp submodule is initialized and checked out.
#
# Previously this script cloned a vendored fork into third_party/. Now
# llama.cpp lives as a git submodule at <repo-root>/llama.cpp/ and this
# script simply ensures it has been initialized.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
DST="$REPO_ROOT/llama.cpp"

cyan()  { printf "\033[36m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
red()   { printf "\033[31m%s\033[0m\n" "$*" >&2; }

if [[ -d "$DST/.git" ]] || [[ -f "$DST/.git" ]]; then
  cyan "==> llama.cpp submodule 已存在，同步中..."
  git -C "$REPO_ROOT" submodule update --init llama.cpp
else
  cyan "==> 初始化 llama.cpp submodule..."
  git -C "$REPO_ROOT" submodule update --init --depth 1 llama.cpp
fi

green "==> llama.cpp 已就绪：$(git -C "$DST" rev-parse --short HEAD)"
