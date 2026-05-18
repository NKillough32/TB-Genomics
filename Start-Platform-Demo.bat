@echo off
setlocal EnableExtensions

REM Demo-only launcher for backend + GUI.
cd /d "%~dp0"

echo ===============================================
echo TB Platform - DEMO MODE
echo ===============================================
echo WARNING: This mode enables synthetic data seeding and
echo allows non-operational actions for demonstration only.
echo Do NOT use for real public health action.
echo.
set /p CONFIRM=Type DEMO to continue: 
if /I not "%CONFIRM%"=="DEMO" (
  echo Demo mode not enabled. Exiting.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python virtual environment not found at .venv\Scripts\python.exe
  echo.
  echo Run these once from the project root:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r backend\requirements.txt
  echo.
  pause
  exit /b 1
)

if "%DATABASE_URL%"=="" (
  set "DATABASE_URL=postgresql://tb:tb@localhost/tb_surveillance"
)

set "TB_ENABLE_SYNTHETIC_SEEDING=1"
set "TB_ALLOW_NON_OPERATIONAL_ACTIONS=1"

echo Launching backend and GUI in DEMO MODE...

start "TB Backend (DEMO)" cmd /k "cd /d %CD% && set DATABASE_URL=%DATABASE_URL% && set TB_ENABLE_SYNTHETIC_SEEDING=1 && set TB_ALLOW_NON_OPERATIONAL_ACTIONS=1 && .venv\Scripts\python.exe -m uvicorn backend.app:app --reload"
start "TB GUI" cmd /k "cd /d %CD%\gui && python -m http.server 8081"

timeout /t 2 /nobreak >nul
start "" "http://localhost:8081"

echo.
echo [DONE] Platform started in DEMO MODE.
echo Remember to use Start-Platform.bat for operational mode.
endlocal

