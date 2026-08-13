@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 검증 파이프라인과 OCR 전용 환경을 설치합니다.
py -3.11 -m venv work\.venv
if errorlevel 1 goto :failed
work\.venv\Scripts\python.exe -m pip install -U pip
if errorlevel 1 goto :failed
work\.venv\Scripts\python.exe -m pip install -e ".[test]"
if errorlevel 1 goto :failed
py -3.11 -m venv work\.venv-ocr
if errorlevel 1 goto :failed
work\.venv-ocr\Scripts\python.exe -m pip install -U pip
if errorlevel 1 goto :failed
work\.venv-ocr\Scripts\python.exe -m pip install -e ".[ocr]"
if errorlevel 1 goto :failed
work\.venv-ocr\Scripts\python.exe localize.py ocr setup
if errorlevel 1 goto :failed
echo.
echo 설치와 OCR 모델 준비가 끝났어요. 1_추출.bat을 실행하세요.
pause
exit /b 0

:failed
echo.
echo 설치가 실패했어요. 위 오류를 해결하기 전에는 다음 단계로 진행하지 않아요.
pause
exit /b 1
