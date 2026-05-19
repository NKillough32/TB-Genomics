@echo off
setlocal EnableExtensions

REM Demo-only backend launcher with explicit confirmation.
cd /d "%~dp0"

echo ===============================================
echo TB Backend - DEMO MODE
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

echo Starting TB backend in DEMO MODE on http://localhost:8000
echo DATABASE_URL=%DATABASE_URL%
echo TB_ENABLE_SYNTHETIC_SEEDING=%TB_ENABLE_SYNTHETIC_SEEDING%
echo TB_ALLOW_NON_OPERATIONAL_ACTIONS=%TB_ALLOW_NON_OPERATIONAL_ACTIONS%
echo.

".venv\Scripts\python.exe" -m uvicorn backend.app:app --reload --reload-dir backend

endlocal

