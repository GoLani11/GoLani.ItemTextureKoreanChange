@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto :failed
.venv\Scripts\python.exe -m pip install -e ".[test]"
if errorlevel 1 goto :failed
echo 기본 도구 설치 완료. 서버 패키징에는 .NET SDK 10이 필요합니다.
exit /b 0
:failed
echo 설치에 실패했습니다.
exit /b 1
