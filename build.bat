@echo off
REM Build Adrenalift with PyInstaller (onedir)
REM Requires: pip install -r requirements.txt
REM Driver files (inpoutx64.dll, WinRing0x64.dll, etc.) must be in drivers/ before building

cd /d "%~dp0"

REM Prefer python, fall back to py launcher (common on Windows)
where python >nul 2>nul && set PY=python || set PY=py

echo Checking dependencies...
%PY% -c "import PySide6, pyinstaller" 2>nul || (
    echo Installing requirements...
    %PY% -m pip install -r requirements.txt
)

echo.
echo Cleaning previous build cache...
if exist build rmdir /s /q build

echo Building with PyInstaller...
%PY% -m PyInstaller --noconfirm --clean build.spec

if %ERRORLEVEL% EQU 0 (
    echo.
    echo Build complete. Output: dist\Adrenalift.exe
    echo Run: dist\Adrenalift.exe ^(single file, ready to share^)
    echo.
    echo Note: Add bios\vbios.rom for VBIOS, or the app will prompt to select one.
) else (
    echo Build failed.
    exit /b 1
)
