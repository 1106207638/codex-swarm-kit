@echo off
setlocal
where py.exe >nul 2>nul
if errorlevel 1 goto usepython
py -3 -B -X utf8 "%~dp0local_agent.py" configure
goto done
:usepython
python -B -X utf8 "%~dp0local_agent.py" configure
:done
set "LOCAL_WORKER_EXIT=%ERRORLEVEL%"
pause
exit /b %LOCAL_WORKER_EXIT%
