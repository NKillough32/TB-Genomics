@echo off
setlocal EnableExtensions

REM One-click first-time bootstrap and launch (backend + GUI).
cd /d "%~dp0"

echo ===============================================
echo TB Genomic Surveillance - Setup and Start
echo ===============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python is not installed or not on PATH.
  echo Install Python 3.10+ and re-run this file.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [INFO] Creating virtual environment at .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
  )
)

echo [INFO] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
  echo [WARN] pip upgrade failed. Continuing with existing pip.
)

echo [INFO] Installing backend dependencies ...
".venv\Scripts\python.exe" -m pip install -r backend\requirements.txt
if errorlevel 1 (
  echo [ERROR] Dependency installation failed.
  pause
  exit /b 1
)

if "%DATABASE_URL%"=="" (
  set "DATABASE_URL=postgresql://tb:tb@localhost:5433/tb_surveillance"
)

echo [INFO] Launching backend and GUI in new windows ...
start "TB Backend" cmd /k "cd /d %CD% && set DATABASE_URL=%DATABASE_URL% && .venv\Scripts\python.exe -m uvicorn backend.app:app --reload"
start "TB GUI" cmd /k "cd /d %CD%\gui && python -m http.server 8081"

timeout /t 2 /nobreak >nul
start "" "http://localhost:8081"

echo.
echo [DONE] If backend fails, check database setup and DATABASE_URL.
endlocal

