# Lineage/DR Validation Tools - Troubleshooting & Fix

## Problem Summary
TBProfiler and Mykrobe tools are not executing during the lineage/DR validation pipeline. On data refresh, the system reverts to warnings about tool unavailability.

## Root Causes Identified

### 1. **TBProfiler Local Execution - BROKEN**
- **Issue**: `ModuleNotFoundError: No module named 'pysam'`
- **Why**: pysam package fails to build from source on Windows (missing C build tools like make, libbz2)
- **Status**: Cannot be fixed without Windows developer environment or conda

### 2. **Mykrobe - NOT INSTALLED**
- **Status**: `not_installed` - not found in PATH
- **Why**: Mykrobe is not available on PyPI for pip installation

### 3. **Docker Fallback - AVAILABLE BUT DAEMON NOT RUNNING**
- **Status**: Docker Desktop is installed but not actively running
- **Solution**: Start Docker Desktop application

### 4. **WSL Fallback - AVAILABLE BUT NOT ENABLED**
- **Status**: WSL2 (Ubuntu-22.04) is available but fallback not enabled
- **Solution**: Set `TBPROFILER_WSL_FALLBACK=1` and verify tools in WSL environment

---

## Solution Path A: Use Docker Fallback (Recommended - Easiest)

### Requirements
- Docker Desktop installed ✓ (already on system)
- Docker daemon running ✗ (needs to be started)

### Steps

1. **Start Docker Desktop**
   - Click Windows Start menu
   - Search for "Docker Desktop"
   - Open the application
   - Wait for daemon to start (check system tray for Docker icon)
   - Verify daemon is running: `docker ps` (should succeed)

2. **Run validation with Docker fallback**
   ```powershell
   $env:TBPROFILER_DOCKER_FALLBACK='1'
   python scripts/run_lineage_dr_validation.py
   ```

3. **Expected behavior**
   - Script will pull `quay.io/jodyphelan/tbprofiler:latest` image (first time)
   - TBProfiler will execute inside Docker container
   - Results will be written to `exports/lineage_dr_validation.json`
   - If successful: `"tbprofiler_docker_run": {"status": "completed", ...}`

---

## Solution Path B: Use WSL Fallback (Alternative)

### Requirements
- WSL2 with Ubuntu installed ✓ (already configured)
- tb-profiler and mykrobe installed in WSL environment ✗ (needs setup)

### Steps

1. **Open WSL terminal and set up tools**
   ```powershell
   wsl
   ```

2. **Inside WSL, install tb-profiler and mykrobe**
   ```bash
   sudo apt-get update
   # For tb-profiler (via conda or bioconda recommended)
   conda create -n tbtools
   conda activate tbtools
   conda install -c bioconda tb-profiler mykrobe
   # Exit WSL
   exit
   ```

3. **Run validation with WSL fallback**
   ```powershell
   $env:TBPROFILER_WSL_FALLBACK='1'
   $env:TBPROFILER_WSL_ENV='tbtools'  # Match the conda env name
   python scripts/run_lineage_dr_validation.py
   ```

4. **Expected behavior**
   - Script detects tb-profiler and mykrobe in WSL
   - Tools execute inside WSL environment
   - Results written to `exports/lineage_dr_validation.json`
   - If successful: `"tbprofiler_wsl_run": {"status": "completed", ...}`

---

## Enabling Fallbacks Permanently

To automatically enable the fallback on every run, add to your environment or `.env` file:

```
TBPROFILER_DOCKER_FALLBACK=1
# OR
TBPROFILER_WSL_FALLBACK=1
TBPROFILER_WSL_ENV=tbtools
```

### For Windows Batch Script
Add to `Start-Platform.bat`:
```batch
SET TBPROFILER_DOCKER_FALLBACK=1
```

### For PowerShell Profile
Add to `$PROFILE`:
```powershell
$env:TBPROFILER_DOCKER_FALLBACK='1'
```

---

## Verification

After choosing and implementing a solution:

```powershell
# Check lineage_dr_validation.json for success
type exports\lineage_dr_validation.json | ConvertFrom-Json | Select-Object status, -ExpandProperty engines

# Expected output should show:
# tb_profiler status: "installed" (local) or "completed" (docker/wsl)
# mykrobe status: "installed" (wsl) or "not_installed" (if docker only)
```

---

## Status Matrix

| Component | Current | Path A (Docker) | Path B (WSL) |
|-----------|---------|-----------------|-------------|
| tb-profiler | installed_but_unusable | ✓ works in container | ✓ requires setup |
| mykrobe | not_installed | ✗ not in Docker image | ✓ requires setup |
| Daemon/Environment | not ready | needs start | needs setup |
| Estimated time to fix | - | 5-10 min | 20-30 min |

---

## Recommended Action
**Path A (Docker)** is faster:
1. Open Docker Desktop
2. Run: `$env:TBPROFILER_DOCKER_FALLBACK='1'; python scripts/run_lineage_dr_validation.py`
3. Wait for execution to complete
4. Check results in `exports/lineage_dr_validation.json`

---

## Next Steps After Fix
Once tools are running successfully:
1. Re-run the full pipeline: `python backend/job_runner.py`
2. Check `exports/lineage_dr_validation.json` for completed status
3. Verify database imports with: `SELECT COUNT(*) FROM tb_interpretation;`
4. Monitor cluster risk scores in GUI (Step 6+)
