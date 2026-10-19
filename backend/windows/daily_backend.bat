@echo off
rem Desk backend - one autopilot cycle in the foreground (desk cycle): hub snapshots into the cache, the inbox
rem scan through the private adapter, then the EOD run of every recent business date whose inputs changed.
rem desk serve runs the same cycle by itself every minute; this file is for a scheduled task or a manual check.
rem Usage: daily_backend.bat [hub|drop] [YYYY-MM-DD]
rem   hub  (default) pull from the hub; a drop folder (DESK_SNAPSHOT_DROP), when set, is imported too
rem   drop           no hub: only the bundles copied by hand into DESK_SNAPSHOT_DROP
rem   YYYY-MM-DD     also sync and keep current this business date (the dates are found by themselves otherwise)
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
set "DESK_ENV_FILE=%~dp0desk.env"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=hub"
if /i not "%MODE%"=="hub" if /i not "%MODE%"=="drop" (
  echo Usage: daily_backend.bat [hub^|drop] [YYYY-MM-DD]
  exit /b 2
)
set "ARGS="
if /i "%MODE%"=="drop" set "ARGS=--no-hub"
if not "%~2"=="" set "ARGS=%ARGS% --date %~2"
echo desk cycle (%MODE%) %~2
Scripts\desk.exe cycle %ARGS% || (echo CYCLE FAILED - see errors above & exit /b 1)
