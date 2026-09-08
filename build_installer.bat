@echo off
REM Build the app, then wrap it in a Windows installer.
REM
REM Version comes from finder\__init__.py -- bump __version__ there and nowhere
REM else. Windows compares versions to decide what an "upgrade" is, so shipping
REM two different builds under the same number leaves Add/Remove Programs
REM lying about what is installed.
cd /d "%~dp0"

for /f %%v in ('".venv\Scripts\python.exe" -c "import finder;print(finder.__version__)"') do set "VER=%%v"
if "%VER%"=="" (
  echo Could not read __version__ from finder\__init__.py
  goto :fail
)
echo Building version %VER%
echo.

echo [1/2] Building the application...
".venv\Scripts\python.exe" -m PyInstaller --onefile --noconsole ^
  --name "ShopScan" --icon assets/app.ico ^
  --add-data "finder/web/index.html;finder/web" ^
  --hidden-import aiodns --hidden-import pycares ^
  --collect-submodules finder --collect-submodules aiohttp ^
  --noconfirm --clean app.py
if errorlevel 1 goto :fail

echo.
echo [2/2] Building the installer...
REM winget installs Inno per-user by default; fall back to the system paths.
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Inno Setup not found. Install it with:
  echo   winget install --id JRSoftware.InnoSetup
  goto :fail
)
REM DefaultDataDir only pre-fills the wizard on a FIRST install. An upgrade
REM reuses whatever folder was chosen last time, via Inno's previous-data store.
"%ISCC%" /DAppVersion=%VER% /DDefaultDataDir="%~dp0data" installer.iss
if errorlevel 1 goto :fail

echo.
echo Done: %~dp0installer\ShopScan-Setup-%VER%.exe
pause
exit /b 0

:fail
echo.
echo BUILD FAILED
pause
exit /b 1
