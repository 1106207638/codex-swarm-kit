@echo off
setlocal DisableDelayedExpansion
set "SWARM_INTERACTIVE="
if "%~1"=="" set "SWARM_INTERACTIVE=1"
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYLAUNCHER_ALLOW_INSTALL="
if defined CODEX_SWARM_PYTHON goto custom
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if not errorlevel 1 goto launcher
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if not errorlevel 1 goto python
goto missing
:custom
"%CODEX_SWARM_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if errorlevel 1 goto missing
"%CODEX_SWARM_PYTHON%" -B -X utf8 "%~dp0setup.py" %*
set "SWARM_EXIT=%ERRORLEVEL%"
goto finish
:launcher
py -3 -B -X utf8 "%~dp0setup.py" %*
set "SWARM_EXIT=%ERRORLEVEL%"
goto finish
:python
python -B -X utf8 "%~dp0setup.py" %*
set "SWARM_EXIT=%ERRORLEVEL%"
goto finish
:missing
echo Python 3.11+ was not found. Install it from https://www.python.org/downloads/
echo Or set CODEX_SWARM_PYTHON to an existing Python executable. No download was requested by this installer.
set "SWARM_EXIT=2"
:finish
if defined SWARM_INTERACTIVE pause
exit /b %SWARM_EXIT%
