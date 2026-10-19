@echo off
rem Desk backend - the desk API, the Desk Monitor and the Scenario Lab on this machine (Ctrl+C to stop).
rem Usage: run_backend.bat [port]      default port 8000
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
set "DESK_ENV_FILE=%~dp0desk.env"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"
rem open the browser after a few seconds, once the server is up
start "" /b cmd /c "ping -n 6 127.0.0.1 >nul & start http://127.0.0.1:%PORT%/desk-monitor.html"
Scripts\desk.exe serve --port %PORT%
