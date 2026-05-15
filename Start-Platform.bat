@echo off
setlocal

REM One-click launcher for backend + GUI (separate windows).
cd /d "%~dp0"

set "BACKEND_PORT=8000"
set "BACKEND_PID="
for /f %%P in ('powershell -NoProfile -Command "$listener = Get-NetTCPConnection -LocalPort %BACKEND_PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess; if ($listener) { $listener }"') do set "BACKEND_PID=%%P"

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

echo Launching backend and GUI...

if defined BACKEND_PID (
  echo [INFO] Backend is already running on http://localhost:%BACKEND_PORT% (PID %BACKEND_PID%).
  echo [INFO] Skipping a second backend launch.
) else (
  start "TB Backend" cmd /k "cd /d %CD% && set DATABASE_URL=%DATABASE_URL% && .venv\Scripts\python.exe -m uvicorn backend.app:app --reload"
)

start "TB GUI" cmd /k "cd /d %CD%\gui && python -m http.server 8081"

timeout /t 2 /nobreak >nul
start "" "http://localhost:8081"

endlocal
