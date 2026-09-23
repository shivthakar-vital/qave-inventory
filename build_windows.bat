@echo off
rem Build "Qave Inventory.exe" into dist\Qave Inventory\  (run on a Windows PC with Python 3 installed)
cd /d "%~dp0"
if not exist .venv python -m venv .venv
.venv\Scripts\pip install -q -r requirements.txt pyinstaller pillow
.venv\Scripts\pyinstaller --noconfirm --clean --log-level WARN --windowed --name "Qave Inventory" --icon assets\icon.png qave_inventory.py
echo Built dist\Qave Inventory\Qave Inventory.exe
