@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 게임 원본 Texture2D와 실제 Material 연결을 workspace에 기록합니다.
work\.venv\Scripts\python.exe localize.py extract
if errorlevel 1 goto :failed
echo.
echo 추출이 끝났어요. 품목별 OCR과 독립 시각 검토를 진행하세요.
pause
exit /b 0

:failed
echo.
echo 추출 또는 Material 연결 검사가 실패했어요.
pause
exit /b 1
