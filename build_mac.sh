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

echo "✓ Built dist/Qave Inventory.app"

if [ "${1:-}" = "--install" ]; then
    rm -rf "/Applications/Qave Inventory.app"
    cp -R "dist/Qave Inventory.app" /Applications/
    echo "✓ Installed to /Applications — open it from Launchpad or Spotlight"
fi
