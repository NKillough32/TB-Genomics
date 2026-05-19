@echo off
setlocal

REM One-click launcher for backend + GUI with TBProfiler/Mykrobe fallbacks enabled.
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

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Platform-TBTools.ps1"

endlocal
