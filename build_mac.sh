#!/usr/bin/env bash
# Build "Qave Inventory.app" into dist/.  Pass --install to also copy it to /Applications.
set -euo pipefail
cd "$(dirname "$0")"

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt pyinstaller

.venv/bin/pyinstaller --noconfirm --clean --log-level WARN \
    --windowed \
    --name "Qave Inventory" \
    --icon assets/icon.icns \
    --osx-bundle-identifier com.vitalbio.qave-inventory \
    qave_inventory.py

# Stamp the version from qave_inventory.py into the app (shown in Finder → Get Info),
# then re-sign, since editing Info.plist invalidates the signature.
VERSION="$(sed -n 's/^APP_VERSION *= *"\([^"]*\)".*/\1/p' qave_inventory.py)"
PLIST="dist/Qave Inventory.app/Contents/Info.plist"
for key in CFBundleShortVersionString CFBundleVersion; do
    /usr/libexec/PlistBuddy -c "Set :$key $VERSION" "$PLIST" 2>/dev/null ||
        /usr/libexec/PlistBuddy -c "Add :$key string $VERSION" "$PLIST"
done
codesign --force --deep --sign - "dist/Qave Inventory.app" 2>/dev/null

echo "✓ Built dist/Qave Inventory.app (version $VERSION)"

if [ "${1:-}" = "--install" ]; then
    rm -rf "/Applications/Qave Inventory.app"
    cp -R "dist/Qave Inventory.app" /Applications/
    echo "✓ Installed to /Applications — open it from Launchpad or Spotlight"
fi
