@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo work/2_edited/ 의 한글화 PNG를 번들로 만들어 bundles/ 에 저장합니다.
python tools\auto.py repack %1
echo.
echo SPT 런처에서 "임시 파일 삭제" 후 게임을 실행하세요.
pause
