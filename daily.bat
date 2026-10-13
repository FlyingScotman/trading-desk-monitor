@echo off
rem Trading Desk Monitor - daily load after market close.
rem 1) put the day's CSV files (positions, funding, trades, md_*, pnl_official) into the inbox folder
rem 2) run this script (or schedule it with Task Scheduler)
rem 3) then run the EOD calculation (calc service), then build-hist-scenarios (step 3 below, optional here)
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if "%TDM_DB%"=="" set TDM_DB=%~dp0data\tdm.sqlite
if "%TDM_INBOX%"=="" set TDM_INBOX=%~dp0inbox
set PY=python
where python >nul 2>nul || set PY=py -3
%PY% -m tdm --db "%TDM_DB%" load "%TDM_INBOX%" || (echo LOAD FAILED - see errors above & exit /b 1)
if not exist "%TDM_INBOX%\done" mkdir "%TDM_INBOX%\done"
move /y "%TDM_INBOX%\*.csv" "%TDM_INBOX%\done\" >nul
%PY% -m tdm --db "%TDM_DB%" build-hist-scenarios
