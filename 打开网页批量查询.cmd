@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 --version >nul 2>&1
if errorlevel 1 goto missing
py -3 -m pip install .
if errorlevel 1 goto failed
py -3 -m pandao_batch serve --open --workdir "%~dp0运行结果"
if errorlevel 1 goto failed
exit /b 0
:missing
echo 请先安装 Python 3.11 或更高版本，再次打开本文件。
pause
exit /b 2
:failed
echo 本轮未完成，请查看上面的错误与已保存的结论。
pause
exit /b 2
