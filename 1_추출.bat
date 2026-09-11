@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  echo 사용법: 1_추출.bat mayo
  exit /b 1
)
.venv\Scripts\python.exe localize.py prepare %*
exit /b %errorlevel%
