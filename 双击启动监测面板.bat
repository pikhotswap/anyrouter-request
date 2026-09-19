@echo off
title 中转站检测面板
cd /d "%~dp0"

rem ---------- 查找 Python ----------
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo 未检测到 Python, 请先安装:
    echo     https://www.python.org/downloads/
    echo     或在 Microsoft Store 搜索 "Python 3" 安装
    echo.
    pause
    exit /b 1
)

rem ---------- 服务已在运行则直接打开页面 ----------
powershell -NoProfile -Command "try{Invoke-WebRequest -UseBasicParsing http://localhost:8765/ -TimeoutSec 2|Out-Null;exit 0}catch{exit 1}" >nul 2>nul
if %errorlevel%==0 (
    echo 服务已在运行, 直接打开页面...
    start "" http://localhost:8765
    timeout /t 2 /nobreak >nul
    exit /b 0
)

rem ---------- 启动服务, 2 秒后自动打开浏览器 ----------
echo 正在启动中转站检测面板...
start "" /min cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:8765"
%PY% relay_monitor.py

echo.
echo 服务已退出。
pause
