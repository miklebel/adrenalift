@echo off
title Restore Original CPU Power Settings
echo ============================================================
echo   Restore Original CPU Power Settings
echo   AMD Ryzen 7 5700X3D - High Performance Plan
echo ============================================================
echo.
echo This script restores the original values before benchmark tweaks:
echo   - Processor idle: Enabled (C-states active)
echo   - Performance increase threshold: 30%%
echo   - Time check interval: 15ms
echo   - Autonomous activity window: 30000 us
echo   - SystemResponsiveness: 10
echo   - Win32PrioritySeparation: 0x2
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: This script must be run as Administrator.
    echo Right-click and select "Run as administrator".
    pause
    exit /b 1
)

echo [1/6] Re-enabling processor idle (C-states)...
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR IDLEDISABLE 0
if %errorlevel% neq 0 echo        WARNING: Failed to restore IDLEDISABLE

echo [2/6] Restoring performance increase threshold to 30%%...
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PERFINCTHRESHOLD 30
if %errorlevel% neq 0 echo        WARNING: Failed to restore PERFINCTHRESHOLD

echo [3/6] Restoring performance time check interval to 15ms...
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PERFCHECK 15
if %errorlevel% neq 0 echo        WARNING: Failed to restore PERFCHECK

echo [4/6] Restoring autonomous activity window to 30000 us...
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PERFAUTONOMOUSWINDOW 30000
if %errorlevel% neq 0 echo        WARNING: Failed to restore PERFAUTONOMOUSWINDOW

echo [5/6] Restoring SystemResponsiveness to 10...
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile" /v SystemResponsiveness /t REG_DWORD /d 10 /f >nul 2>&1
if %errorlevel% neq 0 echo        WARNING: Failed to restore SystemResponsiveness

echo [6/6] Restoring Win32PrioritySeparation to 0x2...
reg add "HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl" /v Win32PrioritySeparation /t REG_DWORD /d 2 /f >nul 2>&1
if %errorlevel% neq 0 echo        WARNING: Failed to restore Win32PrioritySeparation

echo.
echo Applying active scheme...
powercfg /setactive SCHEME_CURRENT

echo.
echo ============================================================
echo   Original settings restored. CPU will now idle normally.
echo   Power consumption and heat will return to normal levels.
echo ============================================================
echo.
pause
