@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 승인·OCR·마스크·D/N/G·밉·번들·실제 렌더 기록을 검증합니다.
work\.venv\Scripts\python.exe localize.py validate
if errorlevel 1 goto :failed
work\.venv\Scripts\python.exe localize.py derive
if errorlevel 1 goto :failed
work\.venv\Scripts\python.exe localize.py repack
if errorlevel 1 goto :failed
work\.venv\Scripts\python.exe localize.py release
if errorlevel 1 goto :failed
echo.
echo 모든 게이트를 통과한 release가 만들어졌어요. 3_배포.bat으로 설치할 수 있어요.
pause
exit /b 0

:failed
echo.
echo 필수 검증이 실패했어요. 실패한 품목은 release나 배포로 넘어가지 않아요.
pause
exit /b 1
