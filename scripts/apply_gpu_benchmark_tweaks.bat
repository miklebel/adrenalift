@echo off
title Apply GPU Benchmark Tweaks (RX 9060 XT Ramp-Up Fix)
echo ============================================================
echo   GPU Benchmark Performance Tweaks - AMD RX 9060 XT
echo ============================================================
echo.
echo This script reduces GPU clock ramp-up time by disabling
echo power-saving features that cause the GPU to start from low
echo clocks. A reboot or driver restart is required for changes
echo to take effect.
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: This script must be run as Administrator.
    echo Right-click and select "Run as administrator".
    pause
    exit /b 1
)

set "GPU_KEY=HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000"

echo [1/5] Disabling power containment (removes boost clock capping)...
reg add "%GPU_KEY%" /v PP_DisablePowerContainment /t REG_DWORD /d 1 /f >nul 2>&1
if %errorlevel% neq 0 (echo        WARNING: Failed) else (echo        OK)

echo [2/5] Disabling clock stretcher (prevents mid-load clock drops)...
reg add "%GPU_KEY%" /v PP_DisableClockStretcher /t REG_DWORD /d 1 /f >nul 2>&1
if %errorlevel% neq 0 (echo        WARNING: Failed) else (echo        OK)

echo [3/5] Disabling memory clock deep sleep...
reg add "%GPU_KEY%" /v PP_MCLKDeepSleepDisable /t REG_DWORD /d 1 /f >nul 2>&1
if %errorlevel% neq 0 (echo        WARNING: Failed) else (echo        OK)

echo [4/5] Disabling GFX low power state...
reg add "%GPU_KEY%" /v KMD_EnableGFXLowPowerState /t REG_DWORD /d 0 /f >nul 2>&1
if %errorlevel% neq 0 (echo        WARNING: Failed) else (echo        OK)

echo [5/5] Disabling DRM/DMA power gating...
reg add "%GPU_KEY%" /v DisableDrmdmaPowerGating /t REG_DWORD /d 1 /f >nul 2>&1
if %errorlevel% neq 0 (echo        WARNING: Failed) else (echo        OK)

echo.
echo ============================================================
echo   All GPU tweaks applied.
echo   REBOOT REQUIRED for changes to take effect.
echo   Run restore_gpu_original_settings.bat to undo.
echo ============================================================
echo.
pause
