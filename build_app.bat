@echo off
REM Rebuild the desktop app after changing the code.
REM Output: dist\ShopScan.exe  (data stays in data\, never bundled)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m PyInstaller --onefile --noconsole ^
  --name "ShopScan" --icon assets/app.ico ^
  --add-data "finder/web/index.html;finder/web" ^
  --hidden-import aiodns --hidden-import pycares ^
  --collect-submodules finder --collect-submodules aiohttp ^
  --noconfirm --clean app.py
echo.
echo Built: %~dp0dist\ShopScan.exe
pause
