from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from backend.auth import require_roles
from backend.database import SessionLocal, init_db
from backend.routers import (
    analytics,
    case_analysis,
    case_assets,
    case_html_report,
    case_lookup,
    case_overview,
    case_reports,
    cluster_investigations,
    data_management,
    epidemiology,
    ingest,
    jobs,
    reports,
    tool_diagnostics,
)
from backend.runtime_paths import ensure_runtime_dirs
from backend.settings import enforce_startup_safety, load_settings, runtime_safety_findings


@asynccontextmanager
async def lifespan(app: FastAPI):
    enforce_startup_safety()
    ensure_runtime_dirs()
    init_db()
    yield


app = FastAPI(title="NI TB Genomic Surveillance v0.6", lifespan=lifespan)


def _cors_origins() -> list[str]:
    return list(load_settings().cors_origins)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(case_overview.router, dependencies=[Depends(require_roles("viewer"))])
app.include_router(case_analysis.router, dependencies=[Depends(require_roles("analyst"))])
app.include_router(case_html_report.router, dependencies=[Depends(require_roles("analyst"))])
app.include_router(case_assets.router, dependencies=[Depends(require_roles("viewer"))])
app.include_router(case_reports.router, dependencies=[Depends(require_roles("analyst"))])
app.include_router(case_lookup.router, dependencies=[Depends(require_roles("analyst"))])
app.include_router(data_management.router, dependencies=[Depends(require_roles("admin"))])
app.include_router(ingest.router, dependencies=[Depends(require_roles("operator"))])
app.include_router(jobs.router, dependencies=[Depends(require_roles("operator"))])
app.include_router(cluster_investigations.router, dependencies=[Depends(require_roles("analyst"))])
app.include_router(analytics.router, dependencies=[Depends(require_roles("viewer"))])
app.include_router(epidemiology.router, dependencies=[Depends(require_roles("analyst"))])
app.include_router(reports.router, dependencies=[Depends(require_roles("viewer"))])
app.include_router(tool_diagnostics.router, dependencies=[Depends(require_roles("analyst"))])


@app.get("/")
def root():
    return {"status": "running"}


@app.get("/health")
def health():
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"status": "healthy", "database": "ok"}
    finally:
        db.close()


@app.get("/runtime-safety", dependencies=[Depends(require_roles("admin"))])
def runtime_safety():
    settings = load_settings()
    return {
        "deployment_mode": settings.deployment_mode,
        "production": settings.is_production,
        "auth_enabled": settings.auth_enabled,
        "cors_origins": list(settings.cors_origins),
        "findings": runtime_safety_findings(settings),
    }
