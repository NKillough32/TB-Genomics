from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.database import SessionLocal
from backend.runtime_paths import EXPORTS_DIR, LOGS_DIR, UPLOADS_DIR, ensure_runtime_dirs


def _status(ok: bool, label: str, detail: str = "") -> bool:
    marker = "OK" if ok else "FAIL"
    suffix = f" - {detail}" if detail else ""
    print(f"[{marker}] {label}{suffix}")
    return ok


def _find_rscript() -> str | None:
    found = shutil.which("Rscript")
    if found:
        return found
    candidates = sorted(Path(r"C:\Program Files\R").glob(r"R-*\bin\Rscript.exe"), reverse=True)
    if not candidates:
        candidates = sorted(Path(r"C:\Program Files\R").glob(r"R-*\bin\x64\Rscript.exe"), reverse=True)
    return str(candidates[0]) if candidates else None


def _check_python_packages() -> bool:
    required = [
        "fastapi",
        "uvicorn",
        "sqlalchemy",
        "psycopg2",
        "Bio",
        "reportlab",
        "numpy",
        "matplotlib",
        "scipy",
        "networkx",
        "alembic",
    ]
    ok = True
    for package in required:
        ok = _status(importlib.util.find_spec(package) is not None, f"Python package: {package}") and ok
    return ok


def _check_database() -> bool:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return _status(True, "Database connection", os.getenv("DATABASE_URL", "(default DATABASE_URL)"))
    except Exception as exc:
        return _status(False, "Database connection", str(exc))


def _check_runtime_dirs() -> bool:
    ok = True
    ensure_runtime_dirs()
    for label, directory in {
        "exports": EXPORTS_DIR,
        "uploads": UPLOADS_DIR,
        "logs": LOGS_DIR,
    }.items():
        probe = directory / ".posit_readiness_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            ok = _status(True, f"Writable {label} directory", str(directory)) and ok
        except Exception as exc:
            ok = _status(False, f"Writable {label} directory", f"{directory}: {exc}") and ok
    return ok


def _check_r() -> bool:
    rscript = _find_rscript()
    if not rscript:
        return _status(False, "Rscript", "required for real outbreaker2 runs")
    env = os.environ.copy()
    local_r_libs = ROOT / "R_libs"
    if local_r_libs.exists():
        env["R_LIBS_USER"] = str(local_r_libs)
    cmd = [
        rscript,
        "-e",
        "if (nzchar(Sys.getenv('R_LIBS_USER'))) .libPaths(c(Sys.getenv('R_LIBS_USER'), .libPaths())); pkgs <- c('outbreaker2','ape','jsonlite'); missing <- pkgs[!vapply(pkgs, requireNamespace, logical(1), quietly=TRUE)]; if (length(missing)) stop(paste(missing, collapse=', ')); cat('ok')",
    ]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=60, env=env)
    return _status(
        result.returncode == 0,
        "R packages for outbreaker2",
        rscript if result.returncode == 0 else (result.stderr or result.stdout).strip(),
    )


def _check_quarto() -> bool:
    quarto = shutil.which("quarto")
    if not quarto:
        return _status(False, "Quarto CLI", "optional unless publishing Quarto reports")
    result = subprocess.run([quarto, "--version"], capture_output=True, text=True, timeout=30)
    return _status(result.returncode == 0, "Quarto CLI", (result.stdout or result.stderr).strip())


def main() -> int:
    print(f"Python: {sys.version.split()[0]}")
    checks = [
        _status((3, 11) <= sys.version_info[:2] < (3, 12), "Python version", "requires >=3.11,<3.12"),
        _check_python_packages(),
        _check_runtime_dirs(),
        _check_database(),
        _check_r(),
        _check_quarto(),
    ]
    return 0 if all(checks[:-1]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
