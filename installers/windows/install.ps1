# ══════════════════════════════════════════════════════════════════
#  ¢entien¢ — Windows Installer (PowerShell)
#
#  Installs ¢entien¢ as a tray app or headless service using
#  Windows Task Scheduler. Generates SSH keys for remote
#  server monitoring.
#
#  Repository: https://github.com/JoshuaMGoth/centienc
#  Website:    https://joshuagoth.com
#  License:    GNU GPL-3.0
#
#  Usage:
#    powershell -ExecutionPolicy Bypass -File install.ps1
#    powershell -ExecutionPolicy Bypass -File install.ps1 -Service
#    powershell -ExecutionPolicy Bypass -File install.ps1 -Port 8080
#    powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
# ══════════════════════════════════════════════════════════════════

param(
    [switch]$Service,
    [switch]$Tray,
    [switch]$Uninstall,
    [int]$Port = 9090
)

$ErrorActionPreference = "Stop"
$Version = "1.0.0"
$InstallDir = "$env:ProgramFiles\centient"
$DataDir = "$env:ProgramData\centient"
$VenvDir = "$InstallDir\venv"
$TaskName = "centient"
$Mode = if ($Service) { "service" } else { "tray" }

function Write-Info  { Write-Host "  [INFO]  $args" -ForegroundColor Cyan }
function Write-Ok    { Write-Host "  [ OK ]  $args" -ForegroundColor Green }
function Write-Warn  { Write-Host "  [WARN]  $args" -ForegroundColor Yellow }
function Write-Err   { Write-Host "  [FAIL]  $args" -ForegroundColor Red; exit 1 }

# ── Admin check ──────────────────────────────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Err "Please run as Administrator" }

# ══════════════════════════════════════════════════════════════
#  UNINSTALL
# ══════════════════════════════════════════════════════════════
if ($Uninstall) {
    Write-Host ""
    Write-Host "  centient - Uninstalling..." -ForegroundColor Yellow
    Write-Host ""

    # Stop and remove scheduled task
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}
    Write-Ok "Scheduled task removed"

    # Remove install directory
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    Write-Ok "Install directory removed"

    # Ask about data
    if (Test-Path $DataDir) {
        $reply = Read-Host "  Remove monitoring data at ${DataDir}? [y/N]"
        if ($reply -eq "y" -or $reply -eq "Y") {
            Remove-Item -Recurse -Force $DataDir
            Write-Ok "Data removed"
        } else {
            Write-Info "Data preserved at $DataDir"
        }
    }

    # Remove firewall rule
    try { Remove-NetFirewallRule -DisplayName "centient" -ErrorAction SilentlyContinue } catch {}
    Write-Ok "Firewall rule removed"

    Write-Host ""
    Write-Ok "centient uninstalled"
    Write-Host ""
    exit 0
}

# ══════════════════════════════════════════════════════════════
#  INSTALL
# ══════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  ========================================" -ForegroundColor Blue
Write-Host "    centient  Installer v$Version ($Mode mode)" -ForegroundColor Green
Write-Host "  ========================================" -ForegroundColor Blue
Write-Host ""

# ── Check Python ─────────────────────────────────────────────
Write-Info "Checking Python..."
$pythonCmd = $null
foreach ($cmd in @("python3", "python", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python 3\.(\d+)") {
            $minor = [int]$Matches[1]
            if ($minor -ge 10) {
                $pythonCmd = $cmd
                Write-Ok "Found $ver"
                break
            }
        }
    } catch {}
}
if (-not $pythonCmd) {
    Write-Host ""
    Write-Host "  Python 3.10+ is required but not found." -ForegroundColor Yellow
    Write-Host "  Download from: https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "  Make sure to check 'Add Python to PATH' during install." -ForegroundColor Yellow
    Write-Host ""
    Write-Err "Python 3.10+ not found"
}

# ── Create directories ───────────────────────────────────────
Write-Info "Creating directories..."
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
Write-Ok "Directories created"

# ── Create venv & install ────────────────────────────────────
Write-Info "Creating Python virtual environment..."
& $pythonCmd -m venv $VenvDir
& "$VenvDir\Scripts\pip.exe" install --upgrade pip setuptools wheel -q

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$PyprojectPath = Join-Path $ProjectRoot "pyproject.toml"

$extras = if ($Mode -eq "tray") { "[tray]" } else { "" }

if (Test-Path $PyprojectPath) {
    Write-Info "Installing from local source..."
    & "$VenvDir\Scripts\pip.exe" install "${ProjectRoot}${extras}" -q
} else {
    Write-Info "Installing from PyPI..."
    & "$VenvDir\Scripts\pip.exe" install "centient${extras} @ git+https://github.com/JoshuaMGoth/centienc.git" -q
}
Write-Ok "centient installed"

# ── Generate SSH key ─────────────────────────────────────────
$sshDir = "$DataDir\.ssh"
$keyFile = "$sshDir\centient_ed25519"

if (-not (Test-Path $keyFile)) {
    Write-Info "Generating SSH keypair..."
    New-Item -ItemType Directory -Path $sshDir -Force | Out-Null

    # Use ssh-keygen if available (comes with Windows 10+)
    $sshKeygen = Get-Command ssh-keygen -ErrorAction SilentlyContinue
    if ($sshKeygen) {
        & ssh-keygen -t ed25519 -f $keyFile -N '""' -C "centient@$env:COMPUTERNAME" -q
        Write-Ok "SSH keypair generated"
    } else {
        Write-Warn "ssh-keygen not found — install OpenSSH to enable SSH monitoring"
    }
} else {
    Write-Info "SSH key already exists"
}

# ── Create Scheduled Task ────────────────────────────────────
Write-Info "Creating scheduled task..."

$centientArgs = if ($Mode -eq "tray") {
    "-m centient --tray --port $Port --open --data-dir `"$DataDir`""
} else {
    "-m centient --service --host 0.0.0.0 --port $Port --data-dir `"$DataDir`""
}

$Action = New-ScheduledTaskAction `
    -Execute "$VenvDir\Scripts\python.exe" `
    -Argument $centientArgs

$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Principal $Principal `
    -Description "centient Server Monitoring Dashboard" -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Ok "Scheduled task created and started"

# ── Firewall rule ────────────────────────────────────────────
Write-Info "Adding firewall rule..."
New-NetFirewallRule -DisplayName "centient" -Direction Inbound `
    -Protocol TCP -LocalPort $Port -Action Allow `
    -ErrorAction SilentlyContinue | Out-Null
Write-Ok "Firewall rule added for port $Port"

# ── Summary ──────────────────────────────────────────────────
$IP = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.InterfaceAlias -notmatch "Loopback" -and $_.IPAddress -ne "127.0.0.1" } |
    Select-Object -First 1).IPAddress
if (-not $IP) { $IP = "127.0.0.1" }

Write-Host ""
Write-Host "  ========================================" -ForegroundColor Green
Write-Host "    ✓ centient Installed Successfully" -ForegroundColor Green
Write-Host "  ========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Dashboard:    http://${IP}:${Port}" -ForegroundColor Cyan
Write-Host "  Mode:         $Mode"
Write-Host "  Install Dir:  $InstallDir"
Write-Host "  Data Dir:     $DataDir"
Write-Host ""

$pubKeyFile = "${keyFile}.pub"
if (Test-Path $pubKeyFile) {
    $pubKey = Get-Content $pubKeyFile
    Write-Host "  SSH Public Key (add to servers you want to monitor):" -ForegroundColor Yellow
    Write-Host "  $pubKey" -ForegroundColor Cyan
    Write-Host ""
}

Write-Host "  Management:"
Write-Host "    Get-ScheduledTask -TaskName centient   # Check status"
Write-Host "    Start-ScheduledTask -TaskName centient  # Start"
Write-Host "    Stop-ScheduledTask -TaskName centient   # Stop"
Write-Host "    .\install.ps1 -Uninstall               # Remove"
Write-Host ""
Write-Host "  Open http://${IP}:${Port} to run the setup wizard." -ForegroundColor Yellow
Write-Host ""
Write-Host "  Links:" -ForegroundColor White
Write-Host "    GitHub:     https://github.com/JoshuaMGoth/centienc" -ForegroundColor Cyan
Write-Host "    Website:    https://joshuagoth.com" -ForegroundColor Cyan
Write-Host "    License:    GNU GPL-3.0"
Write-Host ""
Write-Host "  A JoshuaGoth Software" -ForegroundColor Green
Write-Host ""
