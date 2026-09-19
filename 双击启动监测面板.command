#!/bin/bash
# 双击即可启动中转站监测面板并自动打开浏览器
cd "$(dirname "$0")" || exit 1

# 如果服务已经在跑, 直接打开页面
if curl -s -o /dev/null -m 2 "http://localhost:8765/"; then
  open "http://localhost:8765"
  exit 0
fi

# 延迟 1.5 秒后自动打开浏览器, 前台启动服务(关掉终端窗口即退出)
( sleep 1.5 && open "http://localhost:8765" ) &
exec python3 relay_monitor.py
