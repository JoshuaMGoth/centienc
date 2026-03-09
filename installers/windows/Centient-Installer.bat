@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "LAUNCHER=%SCRIPT_DIR%install.cmd"

if not exist "%LAUNCHER%" (
  echo [ERROR] Could not find install.cmd in "%SCRIPT_DIR%"
  pause
  exit /b 1
)

call "%LAUNCHER%" %*
exit /b %ERRORLEVEL%
