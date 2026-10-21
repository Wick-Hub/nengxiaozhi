@echo off
chcp 65001 >nul
set "PATH=E:\work_space\tools\node-v24.18.0-win-x64;%PATH%"
cd /d %~dp0

echo ==========================================
echo   能小智 · 对话 Web 界面
echo   启动后浏览器打开 http://127.0.0.1:3000
echo ==========================================
echo.
echo 提示：请先确保 MySQL 已启动（运行 tools 目录下的 启动MySQL.bat）
echo 关闭本窗口即停止服务。
echo.

node src\web\server.js
pause
