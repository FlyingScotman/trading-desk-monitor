@echo off
rem Desk backend - offline install from the wheelhouse next to this file (no internet needed).
rem This folder becomes the venv and the backend's root: data in .devdata\, settings in desk.env.
rem Re-run after unpacking a newer bundle over this folder; desk.env and .devdata\ are kept.
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
cd /d "%~dp0"
set "DESK_ENV_FILE=%~dp0desk.env"
py -3.11 -c "import sys; print('Python', sys.version.split()[0], 'OK')" || (
  echo Python 3.11 not found by the py launcher. Install Python 3.11 from python.org or the company software portal.
  exit /b 1
)
rem the embedded store's postgres.exe runs from the pgserver package: stop it before reinstalling
if exist ".devdata\desk_pg\postmaster.pid" if exist "Lib\site-packages\pgserver\pginstall\bin\pg_ctl.exe" (
  "Lib\site-packages\pgserver\pginstall\bin\pg_ctl.exe" stop -D ".devdata\desk_pg" -m fast
)
if not exist "Scripts\python.exe" (
  py -3.11 -m venv "%~dp0." || exit /b 1
)
Scripts\python.exe -m pip install --no-index --find-links wheelhouse -r wheelhouse\requirements.txt || exit /b 1
rem our own wheels keep their version between builds: reinstall them from this bundle
Scripts\python.exe -m pip install --no-index --find-links wheelhouse --no-deps --force-reinstall desk-backend trading-desk-monitor rub-fixed-income-core || exit /b 1
rem desk serve looks for prototypes\ and frontend\dist\ in its root: it must be this folder
Scripts\python.exe -c "import sys, pathlib, desk.settings as s; sys.exit(0 if s.REPO_ROOT == pathlib.Path(sys.argv[1]).resolve() else f'desk root is {s.REPO_ROOT}, not this folder')" "%~dp0." || exit /b 1
if not exist desk.env (
  copy desk.env.example desk.env >nul
  echo Created desk.env from desk.env.example: fill it in, see INSTALL_BACKEND_WINDOWS.md
)
Scripts\desk.exe config || exit /b 1
Scripts\desk.exe db-migrate || exit /b 1
echo.
echo Done. Start the server with run_backend.bat, then open http://127.0.0.1:8000/desk-monitor.html
