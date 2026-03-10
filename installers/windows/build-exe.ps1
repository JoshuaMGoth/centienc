param(
    [string]$Version = "1.0.0",
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$issFile = Join-Path $scriptDir "centienc-installer.iss"

if (-not (Test-Path $issFile)) {
    throw "Inno Setup script not found: $issFile"
}

$possibleIscc = @(
    (Get-Command iscc -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) }

if (-not $possibleIscc -or $possibleIscc.Count -eq 0) {
    throw "ISCC.exe not found. Install Inno Setup 6 from https://jrsoftware.org/isdl.php"
}

$iscc = $possibleIscc[0]
Write-Host "Using ISCC: $iscc" -ForegroundColor Cyan

$resolvedOutputDir = Join-Path $scriptDir $OutputDir
New-Item -ItemType Directory -Force -Path $resolvedOutputDir | Out-Null

Push-Location $scriptDir
try {
    & $iscc "/DMyAppVersion=$Version" "/O$resolvedOutputDir" "$issFile"
} finally {
    Pop-Location
}

Write-Host "Build complete. Output dir: $resolvedOutputDir" -ForegroundColor Green
