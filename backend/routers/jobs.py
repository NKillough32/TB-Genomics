
from fastapi import APIRouter
from fastapi.responses import FileResponse
from backend.job_runner import run_job, JOBS

router = APIRouter(prefix="/jobs", tags=["jobs"])

@router.post("/run/{job_name}")
def run(job_name: str):
    job_id = run_job(job_name)
    if not job_id:
        return {"error": "Job not allowed"}
    return {"job_id": job_id}

@router.get("/status/{job_id}")
def status(job_id: str):
    return JOBS.get(job_id, {"status": "unknown"})

@router.get("/logs/{job_id}")
def logs(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return {"error": "unknown job"}
    return FileResponse(job["logfile"], filename=f"{job_id}.log")
