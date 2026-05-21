from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _runtime_dir(env_name: str, default_name: str) -> Path:
    configured = os.getenv(env_name, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return PROJECT_ROOT / default_name


EXPORTS_DIR = _runtime_dir("TB_EXPORTS_DIR", "exports")
UPLOADS_DIR = _runtime_dir("TB_UPLOADS_DIR", "uploads")
LOGS_DIR = _runtime_dir("TB_LOGS_DIR", "logs")


def export_path(*parts: str) -> str:
    return str(EXPORTS_DIR.joinpath(*parts))


def upload_path(*parts: str) -> str:
    return str(UPLOADS_DIR.joinpath(*parts))


def log_path(*parts: str) -> str:
    return str(LOGS_DIR.joinpath(*parts))


def ensure_runtime_dirs() -> None:
    for directory in (EXPORTS_DIR, UPLOADS_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
