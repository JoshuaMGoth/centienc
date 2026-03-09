# ──────────────────────────────────────────────────────────────────
#  ¢entient¢ — Windows Installer (PowerShell)
#  Usage:
#    powershell -ExecutionPolicy Bypass -File install.ps1           # Tray mode (default)
#    powershell -ExecutionPolicy Bypass -File install.ps1 --service # Headless service
# ──────────────────────────────────────────────────────────────────

param(
    [switch]$Service,
    [switch]$Tray
)

$ErrorActionPreference = "Stop"
$Version = "1.0.0"
$InstallDir = "$env:ProgramFiles\Centient"
$DataDir = "$env:ProgramData\Centient"
$VenvDir = "$InstallDir\venv"
$Port = 9090
$ServiceName = "Centient"
$Mode = if ($Service) { "service" } elseif ($Tray) { "tray" } else { "tray" }  # Default: tray on Windows

function Write-Info  { Write-Host "[INFO]  $args" -ForegroundColor Cyan }
function Write-Ok    { Write-Host "[OK]    $args" -ForegroundColor Green }
function Write-Err   { Write-Host "[ERROR] $args" -ForegroundColor Red; exit 1 }

# ── Admin check ──────────────────────────────────────────────
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Err "Please run as Administrator"
}

Write-Host ""
Write-Host "  ¢entient¢ — Windows Installer v$Version ($Mode mode)" -ForegroundColor Green
Write-Host ""

# ── Check Python ─────────────────────────────────────────────
Write-Info "Checking Python..."
try {
    $pyVersion = python --version 2>&1
    Write-Ok "Found $pyVersion"
} catch {
    Write-Host ""
    Write-Host "  Python 3.10+ is required but not found." -ForegroundColor Yellow
    Write-Host "  Install from: https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host ""
    Write-Err "Python not found. Install Python 3.10+ and try again."
}

# ── Create directories ───────────────────────────────────────
Write-Info "Creating directories..."
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
Write-Ok "Directories created"

# ── Create venv & install ────────────────────────────────────
Write-Info "Creating Python virtual environment..."
python -m venv $VenvDir
& "$VenvDir\Scripts\pip.exe" install --upgrade pip -q

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$PyprojectPath = Join-Path $ProjectRoot "pyproject.toml"

if (Test-Path $PyprojectPath) {
    Write-Info "Installing from local source..."
    if ($Mode -eq "tray") {
        & "$VenvDir\Scripts\pip.exe" install "$ProjectRoot[tray]" -q
    } else {
        & "$VenvDir\Scripts\pip.exe" install $ProjectRoot -q
    }
} else {
    Write-Info "Installing from PyPI..."
    if ($Mode -eq "tray") {
        & "$VenvDir\Scripts\pip.exe" install "centient[tray]" -q
    } else {
        & "$VenvDir\Scripts\pip.exe" install centient -q
    }
}
Write-Ok "¢entient¢ installed"

# ── Create Windows service using NSSM or Task Scheduler ──────
Write-Info "Creating scheduled task..."

$CentientArgs = if ($Mode -eq "tray") { "--tray --port $Port --open" } else { "--service --host 0.0.0.0 --port $Port" }

$Action = New-ScheduledTaskAction `
    -Execute "$VenvDir\Scripts\python.exe" `
    -Argument "-m centient $CentientArgs --data-dir `"$DataDir`""

$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $ServiceName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "¢entient¢ Server" -Force | Out-Null

Start-ScheduledTask -TaskName $ServiceName
Write-Ok "Scheduled task created and started"

# ── Firewall rule ────────────────────────────────────────────
Write-Info "Adding firewall rule..."
New-NetFirewallRule -DisplayName "¢entient¢" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -ErrorAction SilentlyContinue | Out-Null
Write-Ok "Firewall rule added"

# ── Summary ──────────────────────────────────────────────────
$IP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch "Loopback" } | Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "  ✓ ¢entient¢ Installed!" -ForegroundColor Green
Write-Host ""
Write-Host "  Dashboard:   http://${IP}:${Port}" -ForegroundColor Cyan
Write-Host "  Data Dir:    $DataDir"
Write-Host "  Install Dir: $InstallDir"
Write-Host ""
Write-Host "  Open http://${IP}:${Port} to run the setup wizard." -ForegroundColor Yellow
Write-Host ""
