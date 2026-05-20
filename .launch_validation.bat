@echo off
setlocal enabledelayedexpansion
set DATABASE_URL=postgresql://tb:tb@localhost:5433/tb_surveillance
set TBPROFILER_WSL_ENV=tbtools
set TBPROFILER_WSL_FALLBACK=1
set TBPROFILER_DOCKER_FALLBACK=1
cd /d "C:\Users\Nicho\Desktop\TB-Genomics-main"
"C:\Users\Nicho\Desktop\TB-Genomics-main\.venv\Scripts\python.exe" scripts\run_lineage_dr_validation.py
endlocal
