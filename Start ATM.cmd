@echo off
setlocal
cd /d "%~dp0" || goto :failed

if exist ".venv\Scripts\python.exe" goto :dependencies
where py.exe >nul 2>&1
if errorlevel 1 goto :python
py -3 -m venv .venv || goto :failed
goto :dependencies

:python
python -m venv .venv || goto :failed

:dependencies
".venv\Scripts\python.exe" -c "import flask; from importlib.metadata import version; assert tuple(map(int, version('flask').split('.')[:2])) >= (3, 1)" >nul 2>&1
if not errorlevel 1 goto :run
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :failed

:run
set "ATM_BROWSER=--open-browser"
if /I "%~1"=="--no-browser" set "ATM_BROWSER="
".venv\Scripts\python.exe" backend\app.py %ATM_BROWSER% || goto :failed
exit /b 0

:failed
echo.
echo The ATM could not start. Review the error above. Python 3.10 or later is required.
pause
exit /b 1
