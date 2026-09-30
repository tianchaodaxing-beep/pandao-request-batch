@echo off
cd /d "%~dp0"
py -3 -m pip install .
if errorlevel 1 goto failed
py -3 -m pandao_batch --lang en serve --open
if errorlevel 1 goto failed
exit /b 0
:failed
echo The task did not complete. Review the error above.
pause
exit /b 2
