
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, StreamingResponse
from backend.job_runner import cancel_job, get_job_snapshot, run_job, run_pipeline, PIPELINE_STEPS
from backend.data_safety import enforce_operational_dataset, get_data_safety_status
from backend.routers.dependencies import get_db
from backend.quality_gates import build_workflow_status
from backend.runtime_paths import EXPORTS_DIR
from sqlalchemy.orm import Session
import os
import glob
import zipfile
import io

router = APIRouter(prefix="/jobs", tags=["jobs"])


PUBLIC_HEALTH_ACTION_JOBS = {
    "run_lineage_dr_validation",
    "derive_sequence_clusters",
    "export_outbreaker",
    "run_outbreaker2",
    "compare_clustering_methods",
    "run_clustering",
    "run_secondary_validation",
}

@router.post("/run/{job_name}")
def run(job_name: str, db: Session = Depends(get_db)):
    if job_name in PUBLIC_HEALTH_ACTION_JOBS:
        enforce_operational_dataset(db, f"jobs/run/{job_name}")

    job_id = run_job(job_name)
    if not job_id:
        return {"error": "Job not allowed"}
    return {"job_id": job_id}

@router.post("/run-pipeline")
def run_full_pipeline(db: Session = Depends(get_db)):
    enforce_operational_dataset(db, "jobs/run-pipeline")

    pipeline_id = run_pipeline()
    return {"job_id": pipeline_id, "steps": PIPELINE_STEPS}


@router.get("/data-safety")
def jobs_data_safety(db: Session = Depends(get_db)):
    return get_data_safety_status(db)

@router.get("/status/{job_id}")
def status(job_id: str):
    return get_job_snapshot(job_id) or {"status": "unknown"}


@router.post("/cancel/{job_id}")
def cancel(job_id: str):
    return cancel_job(job_id)

@router.get("/logs/{job_id}")
def logs(job_id: str):
    job = get_job_snapshot(job_id)
    if not job:
        return {"error": "unknown job"}
    return FileResponse(job["logfile"], filename=f"{job_id}.log")

@router.get("/last-run-times")
def last_run_times():
    """Return modification times for key export artifacts so the UI can show data freshness."""
    artifacts = {
        "lineage_dr_validation": EXPORTS_DIR / "lineage_dr_validation.json",
        "sequence_clusters": EXPORTS_DIR / "sequence_clustering_summary.json",
        "outbreaker2": EXPORTS_DIR / "outbreaker_summary.json",
        "cluster_comparison": EXPORTS_DIR / "cluster_method_comparison.json",
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


@router.get("/workflow-status")
def workflow_status(db: Session = Depends(get_db)):
    """Return non-blocking dependency health, workflow stages, and analysis confidence gates."""
    return build_workflow_status(db)

@router.get("/download-all-exports")
def download_all_exports(db: Session = Depends(get_db)):
    """Stream a ZIP of all files in the exports/ directory."""
    enforce_operational_dataset(db, "jobs/download-all-exports")

    exports_dir = str(EXPORTS_DIR)
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
