@echo off
rem Start the QR Tunnel A-end HTTP proxy (Windows VM).
rem Reads a_python and A-side options from config.yaml; extra args pass through.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY_EXE=python"
if not exist "%~dp0config.yaml" goto :launch

set "CFG_PY_LINE="
for /f "usebackq delims=" %%L in (`findstr /b /c:"a_python:" "%~dp0config.yaml"`) do set "CFG_PY_LINE=%%L"
if not defined CFG_PY_LINE goto :launch

set "CFG_PY=!CFG_PY_LINE:*a_python:=!"
for /f "tokens=*" %%V in ("!CFG_PY!") do set "CFG_PY=%%V"
set "CFG_PY=!CFG_PY:"=!"
if not defined CFG_PY goto :launch
set "PY_EXE=!CFG_PY!"

:launch
echo [A] Using python: !PY_EXE!
echo [A] Starting QR Tunnel (A proxy, config=%~dp0config.yaml)...
echo [A] Extra args passed through: %*
"!PY_EXE!" -u a_end\a_proxy.py --config "%~dp0config.yaml" %*

echo.
echo [A] Tunnel exited with errorlevel %errorlevel%.
pause
