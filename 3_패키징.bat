@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  echo 사용법: 3_패키징.bat workspace\items\mayo\job.json
  exit /b 1
)
.venv\Scripts\python.exe localize.py package %*
exit /b %errorlevel%
