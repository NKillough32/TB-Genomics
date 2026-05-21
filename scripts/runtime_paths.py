from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _runtime_dir(env_name: str, default_name: str) -> Path:
    configured = os.getenv(env_name, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return ROOT / default_name


EXPORTS = _runtime_dir("TB_EXPORTS_DIR", "exports")
UPLOADS = _runtime_dir("TB_UPLOADS_DIR", "uploads")
LOGS = _runtime_dir("TB_LOGS_DIR", "logs")


def ensure_runtime_dirs() -> None:
    for directory in (EXPORTS, UPLOADS, LOGS):
        directory.mkdir(parents=True, exist_ok=True)
