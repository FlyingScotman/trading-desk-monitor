@echo off
rem Trading Desk Monitor - one-time setup: check Python, create the database, load demo data.
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if "%TDM_DB%"=="" set TDM_DB=%~dp0data\tdm.sqlite
set PY=python
where python >nul 2>nul || set PY=py -3
%PY% -c "import sys; assert sys.version_info >= (3, 9), sys.version; print('Python', sys.version.split()[0], 'OK')" || (
  echo Python 3.9+ not found. Install Python from python.org or the company software portal.
  exit /b 1
)
%PY% -m tdm --db "%TDM_DB%" init-db || exit /b 1
%PY% -m unittest discover -s tests || echo WARNING: tests failed, see output above
set /p DEMO=Load demo run into the database? [y/N] 
if /i "%DEMO%"=="y" %PY% -m tdm --db "%TDM_DB%" demo
echo.
echo Done. Start the server with run.bat, then open http://localhost:8080/
