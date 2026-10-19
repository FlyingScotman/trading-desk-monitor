@echo off
rem Desk Monitor - any desk command with this bundle's environment, e.g. desk.bat config, desk.bat hub-check.
rem Run it from a command prompt (cmd) in this folder.
rem The logic is in app\deskctl.py, run by the Python bundled in python\ (or by "py -3.11" when absent).
setlocal
set "DM_PY=%~dp0python\python.exe"
set "DM_PYARG="
if not exist "%DM_PY%" (
  where py >nul 2>nul || goto nopython
  set "DM_PY=py"
  set "DM_PYARG=-3.11"
)
if not exist "%~dp0app\deskctl.py" goto incomplete
"%DM_PY%" %DM_PYARG% -I -X utf8 "%~dp0app\deskctl.py" desk %*
set "DM_RC=%ERRORLEVEL%"
exit /b %DM_RC%

:nopython
echo Python not found: python\ is missing from this folder and the py launcher is not installed.
echo Unpack the whole zip again (see INSTALL.txt).
pause
exit /b 1

:incomplete
echo This folder is incomplete: app\deskctl.py is missing. Unpack the whole zip again (see INSTALL.txt).
pause
exit /b 1
