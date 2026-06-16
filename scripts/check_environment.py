#!/usr/bin/env python3
"""Check local runtime readiness for the TB genomics platform."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings import load_settings, runtime_safety_findings, truthy  # noqa: E402


def _check_python() -> dict[str, object]:
    version = sys.version_info
    return {
        "name": "python_3_11",
        "status": "pass" if version.major == 3 and version.minor == 11 else "fail",
        "detail": sys.version.split()[0],
    }


def _check_import(module: str, required: bool = True) -> dict[str, object]:
    available = importlib.util.find_spec(module) is not None
    return {
        "name": f"import_{module}",
        "status": "pass" if available else ("fail" if required else "warn"),
        "detail": "available" if available else "not available",
    }


def _check_command(command: str, required: bool = False) -> dict[str, object]:
    path = shutil.which(command)
    return {
        "name": f"command_{command}",
        "status": "pass" if path else ("fail" if required else "warn"),
        "detail": path or "not found on PATH",
    }


def _check_postgres_reachable(database_url: str) -> dict[str, object]:
    parsed = urlparse(database_url)
    host = parsed.hostname
    port = parsed.port or 5432
    if not host:
        return {
            "name": "postgres_reachable",
            "status": "fail",
            "detail": "DATABASE_URL does not include a host.",
        }

    try:
        with socket.create_connection((host, port), timeout=3):
            return {
                "name": "postgres_reachable",
                "status": "pass",
                "detail": f"{host}:{port}",
            }
    except OSError as exc:
        return {
            "name": "postgres_reachable",
            "status": "fail",
            "detail": f"{host}:{port} unreachable: {exc}",
        }


def _safety_checks() -> list[dict[str, object]]:
    settings = load_settings()
    findings = runtime_safety_findings(settings)
    checks = [
        {
            "name": f"runtime_safety_{finding['code']}",
            "status": "fail" if finding["level"] == "error" else "warn",
            "detail": finding["message"],
        }
        for finding in findings
    ]
    if not checks:
        checks.append(
            {
                "name": "runtime_safety",
                "status": "pass",
                "detail": "No unsafe runtime settings detected.",
            }
        )
    return checks


def main() -> int:
    settings = load_settings()
    checks: list[dict[str, object]] = [
        _check_python(),
        _check_import("psycopg2"),
        _check_postgres_reachable(settings.database_url),
        *_safety_checks(),
    ]

    if settings.allow_mock_outbreaker or truthy(os.getenv("TB_CHECK_OPTIONAL_TOOLS")):
        checks.append(_check_command("Rscript"))
    checks.extend(
        [
            _check_command("tb-profiler"),
            _check_command("mykrobe"),
        ]
    )

    result = {
        "settings": {
            **asdict(settings),
            "auth_tokens": "<configured>" if settings.auth_tokens_configured else "",
            "ingest_api_key": "<configured>" if settings.ingest_api_key else "",
        },
        "checks": checks,
    }
    print(json.dumps(result, indent=2))
    return 1 if any(check["status"] == "fail" for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
