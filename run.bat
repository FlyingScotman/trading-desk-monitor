@echo off
rem Trading Desk Monitor - start the web server (Ctrl+C to stop).
rem Usage: run.bat [port]      default port 8080
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
set PORT=%1
if "%PORT%"=="" set PORT=8080
if "%TDM_DB%"=="" set TDM_DB=%~dp0data\tdm.sqlite
set PY=python
where python >nul 2>nul || set PY=py -3
%PY% -m tdm --db "%TDM_DB%" serve --host 0.0.0.0 --port %PORT%
