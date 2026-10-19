@echo off
rem Desk backend - daily EOD: snapshots into the cache, then a run of the books in the store.
rem Usage: daily_backend.bat [hub|drop] [YYYY-MM-DD]
rem   hub  (default) desk eod --sync: pulls the date and the business date before it from the hub
rem   drop           desk import-drop first: bundles copied by hand into DESK_SNAPSHOT_DROP
rem The default date is the previous weekday; holidays are not known here, pass the date then.
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
set "D=%~2"
if "%D%"=="" for /f "usebackq delims=" %%d in (`Scripts\python.exe -c "import datetime as t; d = t.date.today(); print(d - t.timedelta(days={0: 3, 6: 2}.get(d.weekday(), 1)))"`) do set "D=%%d"
set "SYNC=--sync"
if /i "%MODE%"=="drop" (
  Scripts\desk.exe import-drop || (echo IMPORT FAILED - see errors above & exit /b 1)
  set "SYNC="
)
echo EOD %D% (%MODE%)
Scripts\desk.exe eod --date %D% %SYNC% || (echo EOD FAILED - see errors above & exit /b 1)
