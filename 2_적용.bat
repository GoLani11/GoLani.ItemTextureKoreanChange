@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo work/2_edited/ 의 _D(컬러)만 기준으로 _G/_N 자동생성 후 번들을 만듭니다.
python tools\auto.py build %1
echo.
echo 다음: 3_배포.bat 실행
pause
