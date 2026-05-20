@echo off
setlocal

REM One-click backend launcher for Windows.
cd /d "%~dp0"
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
  set "DATABASE_URL=postgresql://tb:tb@localhost:5433/tb_surveillance"
)

if "%TB_OUTBREAKER_TIMEOUT_SEC%"=="" (
  set "TB_OUTBREAKER_TIMEOUT_SEC=3600"
)

echo Starting TB backend on http://localhost:8000
echo DATABASE_URL=%DATABASE_URL%
echo TB_OUTBREAKER_TIMEOUT_SEC=%TB_OUTBREAKER_TIMEOUT_SEC%
echo.

".venv\Scripts\python.exe" -m uvicorn backend.app:app --reload --reload-dir backend

endlocal

