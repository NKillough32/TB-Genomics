from pathlib import Path
import importlib

import pytest


def test_file_upload_dependency_is_declared():
    requirements = Path("backend/requirements.txt").read_text(encoding="utf-8").splitlines()
    normalized = {line.strip().split("==", 1)[0].lower() for line in requirements if line.strip() and not line.startswith("#")}
    assert "python-multipart" in normalized


def test_migration_dependency_is_declared():
    requirements = Path("backend/requirements.txt").read_text(encoding="utf-8").splitlines()
    normalized = {
        line.strip().split("==", 1)[0].lower()
        for line in requirements
        if line.strip() and not line.startswith("#")
    }
    assert "alembic" in normalized


def test_deployment_and_migration_scaffolding_exists():
    assert Path("Dockerfile").is_file()
    assert Path("docker-compose.yml").is_file()
    assert Path("alembic.ini").is_file()
    assert Path("migrations/env.py").is_file()
    assert Path("migrations/versions/0001_baseline.py").is_file()


def test_ingest_router_imports_with_declared_dependencies():
    pytest.importorskip("multipart")
    module = importlib.import_module("backend.routers.ingest")
    upload_routes = [route for route in module.router.routes if getattr(route, "path", None) == "/ingest/file"]
    assert upload_routes
