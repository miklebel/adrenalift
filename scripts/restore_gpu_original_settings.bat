@echo off
title Restore Original GPU Power Settings (RX 9060 XT)
echo ============================================================
echo   Restore Original GPU Power Settings - AMD RX 9060 XT
echo ============================================================
echo.
echo This script removes the benchmark GPU tweaks by deleting
echo the added registry keys, restoring default driver behavior.
echo A reboot or driver restart is required for changes to take
echo effect.
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: This script must be run as Administrator.
    echo Right-click and select "Run as administrator".
    pause
    exit /b 1
)

set "GPU_KEY=HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000"

echo [1/5] Removing PP_DisablePowerContainment...
reg delete "%GPU_KEY%" /v PP_DisablePowerContainment /f >nul 2>&1
if %errorlevel% neq 0 (echo        Already absent or failed) else (echo        OK)

echo [2/5] Removing PP_DisableClockStretcher...
reg delete "%GPU_KEY%" /v PP_DisableClockStretcher /f >nul 2>&1
if %errorlevel% neq 0 (echo        Already absent or failed) else (echo        OK)

echo [3/5] Removing PP_MCLKDeepSleepDisable...
reg delete "%GPU_KEY%" /v PP_MCLKDeepSleepDisable /f >nul 2>&1
if %errorlevel% neq 0 (echo        Already absent or failed) else (echo        OK)

echo [4/5] Removing KMD_EnableGFXLowPowerState...
reg delete "%GPU_KEY%" /v KMD_EnableGFXLowPowerState /f >nul 2>&1
if %errorlevel% neq 0 (echo        Already absent or failed) else (echo        OK)

echo [5/5] Removing DisableDrmdmaPowerGating...
reg delete "%GPU_KEY%" /v DisableDrmdmaPowerGating /f >nul 2>&1
if %errorlevel% neq 0 (echo        Already absent or failed) else (echo        OK)

echo.
echo ============================================================
echo   GPU registry tweaks removed (restored to driver defaults).
echo   REBOOT REQUIRED for changes to take effect.
echo ============================================================
echo.
pause
