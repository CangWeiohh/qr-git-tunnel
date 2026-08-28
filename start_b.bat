@echo off
rem Start the QR Tunnel B-end forwarder (offline cloud desktop).
rem Reads b_python and B-side options from config.yaml; extra args pass through.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY_EXE=python"
if not exist "%~dp0config.yaml" goto :launch

set "CFG_PY_LINE="
for /f "usebackq delims=" %%L in (`findstr /b /c:"b_python:" "%~dp0config.yaml"`) do set "CFG_PY_LINE=%%L"
if not defined CFG_PY_LINE goto :launch

set "CFG_PY=!CFG_PY_LINE:*b_python:=!"
for /f "tokens=*" %%V in ("!CFG_PY!") do set "CFG_PY=%%V"
set "CFG_PY=!CFG_PY:"=!"
if not defined CFG_PY goto :launch
set "PY_EXE=!CFG_PY!"

:launch
echo [B] Using python: !PY_EXE!
echo [B] Starting QR Tunnel (B forwarder, config=%~dp0config.yaml)...
echo [B] Extra args passed through: %*
"!PY_EXE!" -u b_end\b_tunnel.py --config "%~dp0config.yaml" %*

echo.
echo [B] Tunnel exited with errorlevel %errorlevel%.
pause
