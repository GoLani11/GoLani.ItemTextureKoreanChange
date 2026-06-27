@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo C# DLL을 빌드하고 bundles/ 전체를 SPT user/mods 에 설치합니다.
python tools\auto.py deploy
echo.
echo SPT 런처에서 "임시 파일 삭제" 후 게임을 실행하세요.
pause
