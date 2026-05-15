#!/bin/bash
# Build sayzo_hud_wrapper — the tiny posix_spawn wrapper that sits at
# Sayzo.app/Contents/Frameworks/SayzoHud.app/Contents/MacOS/SayzoHud and
# launches the real sayzo-agent binary with `hud --idle`.
#
# This script is run:
#   * Locally on a Mac for validation testing
#   * From CI (.github/workflows/build.yml) before PyInstaller bundles
#     the helper bundle into Sayzo.app
#
# Usage:
#   bash installer/macos/build_sayzo_hud_wrapper.sh
#       # Builds + installs into ./installer/macos/SayzoHud.app/Contents/MacOS/SayzoHud
#   bash installer/macos/build_sayzo_hud_wrapper.sh /custom/output/dir/
#       # Builds binary as $1/SayzoHud (no helper-bundle install)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$SCRIPT_DIR/sayzo_hud_wrapper.c"
DEFAULT_BUNDLE="$SCRIPT_DIR/SayzoHud.app/Contents/MacOS"

# Where to put the binary
if [ "$#" -ge 1 ]; then
  OUT_DIR="$1"
  IS_BUNDLE_INSTALL=0
else
  OUT_DIR="$DEFAULT_BUNDLE"
  IS_BUNDLE_INSTALL=1
fi
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/SayzoHud"

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

# Ad-hoc sign so it's loadable under Hardened Runtime contexts. CI re-signs
# the whole bundle (including this binary, via --deep) with the real
# Developer ID afterwards.
if command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - "$OUT" 2>&1 | sed 's/^/  codesign: /' || \
    echo "  (codesign warning — will be re-signed by CI)"
fi

if [ "$IS_BUNDLE_INSTALL" = "1" ]; then
  echo
  echo "Installed into helper bundle skeleton:"
  echo "  $SCRIPT_DIR/SayzoHud.app/"
  ls -laR "$SCRIPT_DIR/SayzoHud.app/" | head -20
fi

echo "Done."
