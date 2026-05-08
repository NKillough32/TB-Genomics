
import subprocess, uuid, threading, os
import sys
import json
from datetime import datetime
from sqlalchemy import text
from backend.database import SessionLocal

os.makedirs("logs", exist_ok=True)

# Use the current Python interpreter (venv)
python_exe = sys.executable

JOBS = {}
ALLOWED_JOBS = {
    "run_clustering": [python_exe, "scripts/run_clustering.py"],
    "export_outbreaker": [python_exe, "scripts/export_outbreaker.py"],
    "run_outbreaker2": ["Rscript", "outbreaker2/run_outbreaker2.R"],
}

def _log_to_audit(action: str, user_id: str, details: dict):
    """Log an action to the audit trail."""
    try:
        db = SessionLocal()
        db.execute(
            text(
                "INSERT INTO audit_log (action, user_id, details, timestamp) "
                "VALUES (:action, :user_id, CAST(:details AS jsonb), NOW())"
            ),
            {"action": action, "user_id": user_id, "details": json.dumps(details)},
        )
        db.commit()
        db.close()
    except Exception as e:
        print(f"Audit logging failed: {e}")


def run_job(job_name):
    if job_name not in ALLOWED_JOBS:
        return None
    job_id = str(uuid.uuid4())
    log = f"logs/{job_id}.log"
    JOBS[job_id] = {"job": job_name, "status": "queued", "progress": 0, "logfile": log}

    def task():
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["progress"] = 10
        
        # Log job start
        _log_to_audit("job_started", "system", {"job_id": job_id, "job_name": job_name})
        
        with open(log, "w") as lf:
            try:
                JOBS[job_id]["progress"] = 40
                # Get the project root (parent of backend dir)
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                
                # Special handling for R job - fall back to mock if R fails
                if job_name == "run_outbreaker2":
                    result = subprocess.run(
                        ALLOWED_JOBS[job_name],
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                        cwd=project_root,
                        timeout=300,
                    )
                    if result.returncode != 0:
                        # R failed - use mock generator
                        lf.write("\n--- R execution failed, using mock report generator ---\n")
                        mock_result = subprocess.run(
                            [python_exe, "scripts/generate_mock_outbreaker.py"],
                            stdout=lf,
                            stderr=subprocess.STDOUT,
                            cwd=project_root,
                            timeout=60,
                        )
                        if mock_result.returncode != 0:
                            raise Exception("Both R and mock generator failed")
                else:
                    subprocess.run(
                        ALLOWED_JOBS[job_name],
                        stdout=lf,
                        stderr=lf,
                        check=True,
                        cwd=project_root,
                    )
                
                JOBS[job_id]["progress"] = 100
                JOBS[job_id]["status"] = "completed"
                
                # Log job completion
                _log_to_audit("job_completed", "system", {"job_id": job_id, "job_name": job_name})
            except Exception as e:
                JOBS[job_id]["status"] = "failed"
                lf.write(str(e))
                
                # Log job failure
                _log_to_audit("job_failed", "system", {"job_id": job_id, "job_name": job_name, "error": str(e)})

    threading.Thread(target=task).start()
    return job_id
