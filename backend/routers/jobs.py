
from fastapi import APIRouter
from fastapi.responses import FileResponse, StreamingResponse
from backend.job_runner import run_job, run_pipeline, JOBS, PIPELINE_STEPS
import os
import glob
import zipfile
import io

router = APIRouter(prefix="/jobs", tags=["jobs"])

@router.post("/run/{job_name}")
def run(job_name: str):
    job_id = run_job(job_name)
    if not job_id:
        return {"error": "Job not allowed"}
    return {"job_id": job_id}

@router.post("/run-pipeline")
def run_full_pipeline():
    pipeline_id = run_pipeline()
    return {"job_id": pipeline_id, "steps": PIPELINE_STEPS}

@router.get("/status/{job_id}")
def status(job_id: str):
    return JOBS.get(job_id, {"status": "unknown"})

@router.get("/logs/{job_id}")
def logs(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return {"error": "unknown job"}
    return FileResponse(job["logfile"], filename=f"{job_id}.log")

@router.get("/last-run-times")
def last_run_times():
    """Return modification times for key export artifacts so the UI can show data freshness."""
    artifacts = {
        "lineage_dr_validation": "exports/lineage_dr_validation.json",
        "sequence_clusters": "exports/sequence_clustering_summary.json",
        "outbreaker2": "exports/outbreaker_summary.json",
        "cluster_comparison": "exports/cluster_method_comparison.json",
    }
    result = {}
    for key, path in artifacts.items():
        if os.path.exists(path):
            mtime = os.path.getmtime(path)
            from datetime import datetime, timezone
            result[key] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        else:
            result[key] = None
    return result

@router.get("/download-all-exports")
def download_all_exports():
    """Stream a ZIP of all files in the exports/ directory."""
    exports_dir = "exports"
    if not os.path.isdir(exports_dir):
        return {"error": "exports directory not found"}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filepath in glob.glob(os.path.join(exports_dir, "**", "*"), recursive=True):
            if os.path.isfile(filepath):
                arcname = os.path.relpath(filepath, exports_dir)
                zf.write(filepath, arcname)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=tb_genomics_exports.zip"},
    )
