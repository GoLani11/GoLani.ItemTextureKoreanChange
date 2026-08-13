@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 최신 해시 고정 release만 백업 후 SPT에 설치합니다.
work\.venv\Scripts\python.exe localize.py deploy --release latest --execute
if errorlevel 1 goto :failed
echo.
echo 설치가 끝났어요. SPT 런처에서 임시 파일 삭제 후 실행하세요.
pause
exit /b 0

:failed
echo.
echo 검증된 release가 없거나 설치 검증이 실패했어요. 기존 설치는 백업돼요.
pause
exit /b 1
