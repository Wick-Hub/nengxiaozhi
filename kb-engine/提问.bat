@echo off
chcp 65001 >nul
set "PATH=E:\work_space\tools\node-v24.18.0-win-x64;%PATH%"
cd /d %~dp0

if "%~1"=="" (
  echo 用法: 提问.bat "你的问题" [--role 本科生^|研究生^|教职工^|离退休^|访客] [--mode hybrid^|vector^|keyword]
  echo.
  echo 示例:
  echo   提问.bat "住宿费怎么退" --role 本科生
  echo   提问.bat "差旅费报销标准" --role 教职工 --mode hybrid
  pause
  exit /b
)

node src/query.js %*
pause
