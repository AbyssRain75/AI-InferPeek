@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 首次运行，正在创建 Python 环境...
    python -m venv .venv || goto :fail
)
rem 依赖装好后把 requirements.txt 复制一份当标记：上次安装中途失败（比如断网）时没有它，依赖有变化时对不上，这两种都重新装
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo 正在安装依赖...
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :fail
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

".venv\Scripts\python.exe" -m aiinferpeek %*
goto :eof

:fail
echo 安装失败。请确认已安装 Python 3.10 或更高版本，并且能联网。
pause
