#!/usr/bin/env bash
# Download the Restate server + CLI binaries and sync Python deps.
# No docker / node / brew on this box, so we take the standalone musl binaries.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$REPO_ROOT/bin"
VERSION="${RESTATE_VERSION:-v1.7.8}"

case "$(uname -m)" in
  aarch64|arm64) ARCH=aarch64 ;;
  x86_64|amd64)  ARCH=x86_64 ;;
  *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;;
esac
TARGET="${ARCH}-unknown-linux-musl"
BASE="https://github.com/restatedev/restate/releases/download/${VERSION}"

mkdir -p "$BIN_DIR"

fetch() {
  local component="$1" binname="$2"
  if [[ -x "$BIN_DIR/$binname" ]]; then
    echo "==> $binname already present, skipping"
    return
  fi
  local tarball="${component}-${TARGET}.tar.xz"
  echo "==> downloading $tarball"
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  curl -fsSL --retry 3 -o "$tmp/$tarball" "$BASE/$tarball"
  curl -fsSL --retry 3 -o "$tmp/$tarball.sha256" "$BASE/$tarball.sha256"
  ( cd "$tmp" && awk '{print $1"  '"$tarball"'"}' "$tarball.sha256" | sha256sum -c - )
  tar -xJf "$tmp/$tarball" -C "$tmp"
  # tarballs may or may not have a top-level dir; find the binary either way
  find "$tmp" -type f -name "$binname" -perm -u+x -exec cp {} "$BIN_DIR/$binname" \; -quit
  [[ -x "$BIN_DIR/$binname" ]] || { echo "failed to extract $binname" >&2; exit 1; }
  echo "==> installed $BIN_DIR/$binname"
}

fetch restate-server restate-server
fetch restate-cli    restate

echo "==> syncing python deps"
cd "$REPO_ROOT"
uv sync

echo
echo "Done."
"$BIN_DIR/restate-server" --version
"$BIN_DIR/restate" --version
