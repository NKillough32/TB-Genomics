from contextlib import asynccontextmanager
import os

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.auth import require_roles
from backend.database import init_db
from backend.routers import (
    analytics,
    case_analysis,
    case_assets,
    case_lookup,
    case_overview,
    case_reports,
    cases,
    cluster_investigations,
    epidemiology,
    ingest,
    jobs,
    reports,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="NI TB Genomic Surveillance v0.6", lifespan=lifespan)


def _cors_origins() -> list[str]:
    configured = os.getenv("TB_CORS_ORIGINS", "").strip()
    if configured:
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    return [
        "http://localhost:8081",
        "http://127.0.0.1:8081",
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(case_overview.router, dependencies=[Depends(require_roles("viewer"))])
app.include_router(cases.router, dependencies=[Depends(require_roles("analyst"))])
app.include_router(case_analysis.router, dependencies=[Depends(require_roles("analyst"))])
app.include_router(case_assets.router, dependencies=[Depends(require_roles("viewer"))])
app.include_router(case_reports.router, dependencies=[Depends(require_roles("analyst"))])
app.include_router(case_lookup.router, dependencies=[Depends(require_roles("analyst"))])
app.include_router(ingest.router, dependencies=[Depends(require_roles("operator"))])
app.include_router(jobs.router, dependencies=[Depends(require_roles("operator"))])
app.include_router(cluster_investigations.router, dependencies=[Depends(require_roles("analyst"))])
app.include_router(analytics.router, dependencies=[Depends(require_roles("viewer"))])
app.include_router(epidemiology.router, dependencies=[Depends(require_roles("analyst"))])
app.include_router(reports.router, dependencies=[Depends(require_roles("viewer"))])


@app.get("/")
def root():
    return {"status": "running"}
