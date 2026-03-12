@echo off
:: CentienC — Windows Installer Launcher
:: Elevates to admin and runs install.ps1

:: Check admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

:: Run installer
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
