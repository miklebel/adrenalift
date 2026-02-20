# Build RDNA4 Overclock GUI with PyInstaller (onedir)
# Requires: pip install -r requirements.txt
# Driver files (inpoutx64.dll, WinRing0x64.dll, etc.) must be in drivers/ before building

Set-Location $PSScriptRoot

# Prefer python, fall back to py launcher (common on Windows)
$py = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { "py" }

Write-Host "Checking dependencies..."
try {
    & $py -c "import PySide6, pyinstaller" 2>$null
} catch {
    Write-Host "Installing requirements..."
    & $py -m pip install -r requirements.txt
}

Write-Host ""
Write-Host "Building with PyInstaller..."
& $py -m PyInstaller --noconfirm build.spec

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "Build complete. Output: dist\RDNA4_Overclock.exe"
    Write-Host "Run: dist\RDNA4_Overclock.exe (single file, ready to share)"
    Write-Host ""
    Write-Host "Note: Add bios\vbios.rom for VBIOS, or the app will prompt to select one."
} else {
    Write-Host "Build failed."
    exit 1
}
