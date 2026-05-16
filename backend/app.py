from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.database import init_db
from backend.routers import (
    analytics,
    case_assets,
    case_lookup,
    case_overview,
    case_reports,
    cases,
    cluster_investigations,
    ingest,
    jobs,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="NI TB Genomic Surveillance v0.6", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8081",
        "http://127.0.0.1:8081",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(case_overview.router)
app.include_router(cases.router)
app.include_router(case_assets.router)
app.include_router(case_reports.router)
app.include_router(case_lookup.router)
app.include_router(ingest.router)
app.include_router(jobs.router)
app.include_router(cluster_investigations.router)
app.include_router(analytics.router)


@app.get("/")
def root():
    return {"status": "running"}
