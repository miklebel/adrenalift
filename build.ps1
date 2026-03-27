# Build Adrenalift with PyInstaller (onefile)
# Requires: pip install -r requirements.txt
# Driver files (inpoutx64.dll, WinRing0x64.dll, etc.) must be in drivers/ before building

Set-Location $PSScriptRoot

# ---------------------------------------------------------------------------
# Bump build number in version.json
# ---------------------------------------------------------------------------
$versionFile = Join-Path $PSScriptRoot "version.json"
$versionData = Get-Content $versionFile -Raw | ConvertFrom-Json
$versionData.build = $versionData.build + 1
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($versionFile, ($versionData | ConvertTo-Json), $utf8NoBom)
$ver   = $versionData.version
$build = $versionData.build
$exeName = "Adrenalift_${ver}_${build}"
Write-Host "Version $ver  Build $build  ->  $exeName.exe"

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
Write-Host "Cleaning previous build cache..."
$buildDir = Join-Path $PSScriptRoot "build"
if (Test-Path $buildDir) { Remove-Item $buildDir -Recurse -Force }

Write-Host "Building with PyInstaller..."
& $py -m PyInstaller --noconfirm --clean build.spec

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "Build complete. Output: dist\$exeName.exe"
    Write-Host "Run: dist\$exeName.exe (single file, ready to share)"
    Write-Host ""
    Write-Host "Note: Add bios\vbios.rom for VBIOS, or the app will prompt to select one."
} else {
    Write-Host "Build failed."
    exit 1
}
