#!/usr/bin/env bash
# Put the Pyodide runtime the web app needs under web/public/pyodide/.
#
# The browser build runs the real Python engine (backend/src/backoffice) with
# Pyodide. We self-host the few files it needs instead of loading them from a
# CDN, and never commit them (they are ~15 MB).
#
# Sources, in order:
#   1. PYODIDE_LOCAL_DIR   a directory that already holds the files (local dev)
#   2. PYODIDE_TARBALL     a local copy of pyodide-$VERSION.tar.bz2
#   3. the GitHub release  https://github.com/pyodide/pyodide/releases/download/$VERSION/pyodide-$VERSION.tar.bz2
#
# Usage: backend/scripts/fetch_pyodide.sh [dest-dir]
set -euo pipefail

VERSION="${PYODIDE_VERSION:-314.0.7}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEST="${1:-$REPO/web/public/pyodide}"
URL="https://github.com/pyodide/pyodide/releases/download/${VERSION}/pyodide-${VERSION}.tar.bz2"

# Runtime files plus pydantic and its dependencies (the only third-party
# packages the engine imports in the browser).
CORE=(pyodide.mjs pyodide.asm.mjs pyodide.asm.wasm python_stdlib.zip pyodide-lock.json)
WHEELS=(
  'pydantic-*.whl'
  'pydantic_core-*.whl'
  'typing_extensions-*.whl'
  'typing_inspection-*.whl'
  'annotated_types-*.whl'
)

have_all() {
  local dir="$1" f
  for f in "${CORE[@]}"; do [[ -s "$dir/$f" ]] || return 1; done
  for f in "${WHEELS[@]}"; do compgen -G "$dir/$f" >/dev/null || return 1; done
  [[ "$(cat "$dir/.version" 2>/dev/null)" == "$VERSION" ]] || [[ "${2:-}" == "source" ]] || return 1
}

if have_all "$DEST"; then
  echo "Pyodide $VERSION already in $DEST"
  exit 0
fi

rm -rf "$DEST"
mkdir -p "$DEST"

if [[ -n "${PYODIDE_LOCAL_DIR:-}" ]]; then
  have_all "$PYODIDE_LOCAL_DIR" source || { echo "PYODIDE_LOCAL_DIR=$PYODIDE_LOCAL_DIR is missing files" >&2; exit 1; }
  for f in "${CORE[@]}"; do cp "$PYODIDE_LOCAL_DIR/$f" "$DEST/"; done
  for f in "${WHEELS[@]}"; do cp $PYODIDE_LOCAL_DIR/$f "$DEST/"; done
  echo "Copied Pyodide from $PYODIDE_LOCAL_DIR"
else
  patterns=()
  for f in "${CORE[@]}" "${WHEELS[@]}"; do patterns+=("pyodide/$f"); done
  if [[ -n "${PYODIDE_TARBALL:-}" ]]; then
    echo "Extracting Pyodide $VERSION from $PYODIDE_TARBALL"
    tar -xjf "$PYODIDE_TARBALL" -C "$DEST" --strip-components=1 --wildcards "${patterns[@]}"
  else
    echo "Downloading Pyodide $VERSION from $URL"
    curl -fsSL --retry 3 "$URL" | tar -xj -C "$DEST" --strip-components=1 --wildcards "${patterns[@]}"
  fi
fi

echo "$VERSION" > "$DEST/.version"
have_all "$DEST" || { echo "Pyodide files incomplete in $DEST" >&2; exit 1; }
du -sh "$DEST"
