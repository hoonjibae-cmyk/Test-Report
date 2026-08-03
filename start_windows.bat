@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   OMR 채점 시스템 실행 (목동유쌤영어학원)
echo ============================================================
echo.
echo [1/2] 필요한 프로그램을 설치합니다. (처음 한 번만, 몇 분 걸립니다)
py -m pip install -r requirements.txt || python -m pip install -r requirements.txt
echo.
echo [2/2] 서버를 켭니다.
echo   ▶ 웹브라우저(크롬/엣지)를 열고 주소창에 아래를 입력하세요:
echo.
echo        http://localhost:8000
echo.
echo   (끄려면 이 검은 창에서 Ctrl 키와 C 키를 함께 누르세요)
echo ============================================================
py -m uvicorn webapp.app:app --port 8000 || python -m uvicorn webapp.app:app --port 8000
pause
