@echo off
chcp 65001 >nul
echo 필요한 파이썬 라이브러리를 설치합니다...
pip install "UnityPy==1.25.0" "Pillow>=9.0.0" "texture2ddecoder>=1.0.3" numpy
echo.
echo 설치 끝. 1_추출.bat 을 실행하세요.
pause
