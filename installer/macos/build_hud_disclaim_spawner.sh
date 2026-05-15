#!/bin/bash
# Build hud_disclaim_spawner — the tiny posix_spawn-with-disclaim wrapper
# that the HUD launcher invokes on macOS to break parent-bundle classification.
#
# This script is run:
#   * Locally on a Mac for validation testing (writes to /tmp/ by default)
#   * From CI (.github/workflows/build.yml) before PyInstaller bundles the
#     output binary into Sayzo.app/Contents/MacOS/
#
# Usage:
#   bash installer/macos/build_hud_disclaim_spawner.sh           # builds to ./dist/hud_disclaim_spawner
#   bash installer/macos/build_hud_disclaim_spawner.sh /tmp/     # builds to /tmp/hud_disclaim_spawner

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$SCRIPT_DIR/hud_disclaim_spawner.c"

OUT_DIR="${1:-$(cd "$SCRIPT_DIR/../.." && pwd)/dist}"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/hud_disclaim_spawner"

if ! command -v cc >/dev/null 2>&1; then
  echo "ERROR: cc not found. Install Xcode Command Line Tools:"
  echo "  xcode-select --install"
  exit 1
fi

echo "Compiling: $SOURCE"
echo "Output:    $OUT"

cc -O2 -Wall -Wextra -mmacosx-version-min=11.0 \
   -o "$OUT" \
   "$SOURCE"

echo "Built. File info:"
file "$OUT"
ls -l "$OUT"

# If signing is available, ad-hoc sign so it can be loaded under
# Hardened Runtime contexts. CI re-signs with the real Developer ID
# afterwards.
if command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - "$OUT" 2>&1 | sed 's/^/  codesign: /' || \
    echo "  (codesign warning — will be re-signed by CI)"
fi

echo "Done."
