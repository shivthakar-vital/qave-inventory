#!/bin/bash
# Installs or updates Qave Inventory on this Mac. Run in Terminal:
#   curl -fsSL https://raw.githubusercontent.com/shivthakar-vital/qave-inventory/main/install.sh | bash
set -euo pipefail

APP="Qave Inventory.app"
URL="${QAVE_ZIP_URL:-https://github.com/shivthakar-vital/qave-inventory/releases/latest/download/Qave-Inventory-mac.zip}"
DEST="${QAVE_INSTALL_DIR:-/Applications}"

if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != "1" ]; then
    echo "Sorry, Qave Inventory needs a Mac with Apple Silicon (M1 or newer)."
    exit 1
fi
if [ ! -w "$DEST" ]; then                 # not an admin: install just for this user
    DEST="$HOME/Applications"
    mkdir -p "$DEST"
fi

echo "Downloading Qave Inventory…"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fL --progress-bar "$URL" -o "$TMP/app.zip"
ditto -x -k "$TMP/app.zip" "$TMP"

pkill -f "$APP/Contents/MacOS" 2>/dev/null || true     # close it if it's running (update)
rm -rf "${DEST:?}/$APP"
mv "$TMP/$APP" "$DEST/"
xattr -dr com.apple.quarantine "$DEST/$APP" 2>/dev/null || true

echo "✓ Installed Qave Inventory in $DEST. Opening it now…"
open "$DEST/$APP"
