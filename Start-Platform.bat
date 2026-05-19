@echo off
setlocal

REM One-click launcher for backend + GUI (separate windows).
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

if "%TB_OUTBREAKER_TIMEOUT_SEC%"=="" (
  set "TB_OUTBREAKER_TIMEOUT_SEC=3600"
)

echo Launching backend and GUI...

start "TB Backend" cmd /k "cd /d %CD% && set DATABASE_URL=%DATABASE_URL% && set TB_OUTBREAKER_TIMEOUT_SEC=%TB_OUTBREAKER_TIMEOUT_SEC% && .venv\Scripts\python.exe -m uvicorn backend.app:app --reload --reload-dir backend"

start "TB GUI" cmd /k "cd /d %CD%\gui && python -m http.server 8081"

timeout /t 2 /nobreak >nul
start "" "http://localhost:8081"

endlocal

