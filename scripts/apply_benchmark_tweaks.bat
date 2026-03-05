@echo off
title Apply CPU Benchmark Tweaks (3DMark Ramp-Up Fix)
echo ============================================================
echo   CPU Benchmark Performance Tweaks - AMD Ryzen 7 5700X3D
echo ============================================================
echo.
echo This script eliminates CPU ramp-up time for benchmarking by:
echo   - Disabling processor idle (C-states via OS)
echo   - Setting performance increase threshold to 0%%
echo   - Setting time check interval to 1ms
echo   - Setting autonomous activity window to 0
echo   - Setting SystemResponsiveness to 0
echo   - Setting Win32PrioritySeparation to 0x26
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: This script must be run as Administrator.
    echo Right-click and select "Run as administrator".
    pause
    exit /b 1
)

echo [1/6] Disabling processor idle (prevents C-state sleep)...
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR IDLEDISABLE 1
if %errorlevel% neq 0 echo        WARNING: Failed to set IDLEDISABLE

echo [2/6] Setting performance increase threshold to 0%%...
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PERFINCTHRESHOLD 0
if %errorlevel% neq 0 echo        WARNING: Failed to set PERFINCTHRESHOLD

echo [3/6] Setting performance time check interval to 1ms...
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PERFCHECK 1
if %errorlevel% neq 0 echo        WARNING: Failed to set PERFCHECK

echo [4/6] Setting autonomous activity window to 0...
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PERFAUTONOMOUSWINDOW 0
if %errorlevel% neq 0 echo        WARNING: Failed to set PERFAUTONOMOUSWINDOW

echo [5/6] Setting SystemResponsiveness to 0...
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile" /v SystemResponsiveness /t REG_DWORD /d 0 /f >nul 2>&1
if %errorlevel% neq 0 echo        WARNING: Failed to set SystemResponsiveness

echo [6/6] Setting Win32PrioritySeparation to 0x26...
reg add "HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl" /v Win32PrioritySeparation /t REG_DWORD /d 38 /f >nul 2>&1
if %errorlevel% neq 0 echo        WARNING: Failed to set Win32PrioritySeparation

echo.
echo Applying active scheme...
powercfg /setactive SCHEME_CURRENT

echo.
echo ============================================================
echo   All tweaks applied. CPU will now stay at max clocks.
echo   Idle power and heat will be higher than normal.
echo   Run restore_original_settings.bat to undo these changes.
echo ============================================================
echo.
pause
