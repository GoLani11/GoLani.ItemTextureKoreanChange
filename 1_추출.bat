@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 게임 번들에서 텍스처를 추출합니다 (work/1_raw/).
echo  - 특정 아이템만: 이 창에 필터를 넣어 실행 (예: 1_추출.bat item_food_mayo)
python tools\auto.py extract %1
pause
