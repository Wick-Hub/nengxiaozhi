@echo off
chcp 65001 >nul
set "PATH=E:\work_space\tools\node-v24.18.0-win-x64;%PATH%"
cd /d %~dp0
echo 正在重建知识库索引（解析→切片→向量化→写入 MySQL）...
node src/ingest.js
pause
