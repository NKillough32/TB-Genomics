
from fastapi import FastAPI
from backend.database import init_db
from backend.routers import cases, ingest, jobs

app = FastAPI(title="NI TB Genomic Surveillance v0.6")
init_db()
app.include_router(cases.router)
app.include_router(ingest.router)
app.include_router(jobs.router)

@app.get("/")
def root():
    return {"status": "running"}
