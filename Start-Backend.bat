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
  set "DATABASE_URL=postgresql://tb:tb@localhost/tb_surveillance"
)

echo Starting TB backend on http://localhost:8000
echo DATABASE_URL=%DATABASE_URL%
echo.

".venv\Scripts\python.exe" -m uvicorn backend.app:app --reload --reload-dirs backend

endlocal

