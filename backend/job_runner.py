
import subprocess, uuid, threading, os
import sys
import json
import shutil
import glob
from datetime import datetime
from sqlalchemy import text
from backend.database import SessionLocal

os.makedirs("logs", exist_ok=True)

# Use the current Python interpreter (venv)
python_exe = sys.executable

JOBS = {}
ALLOWED_JOBS = {
    "derive_sequence_clusters": [python_exe, "scripts/derive_sequence_clusters.py"],
    "compare_clustering_methods": [python_exe, "scripts/compare_clustering_methods.py"],
    "run_clustering": [python_exe, "scripts/run_clustering.py"],
    "export_outbreaker": [python_exe, "scripts/export_outbreaker.py"],
    "run_lineage_dr_validation": [python_exe, "scripts/run_lineage_dr_validation.py"],
    "run_outbreaker2": ["Rscript", "outbreaker2/run_outbreaker2.R"],
    "run_secondary_validation": [python_exe, "scripts/run_secondary_engine_validation.py"],
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
        
        with open(log, "w", encoding="utf-8") as lf:
            try:
                JOBS[job_id]["progress"] = 40
                # Get the project root (parent of backend dir)
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                
                # Special handling for R job - fall back to mock if R fails
                if job_name == "run_outbreaker2":
                    rscript_path = shutil.which("Rscript")
                    if not rscript_path:
                        candidates = sorted(glob.glob(r"C:\Program Files\R\R-*\bin\Rscript.exe"), reverse=True)
                        if not candidates:
                            candidates = sorted(glob.glob(r"C:\Program Files\R\R-*\bin\x64\Rscript.exe"), reverse=True)
                        if candidates:
                            rscript_path = candidates[0]
                    use_mock_fallback = False
                    allow_mock_fallback = os.getenv("TB_ALLOW_MOCK_OUTBREAKER", "0") == "1"
                    child_env = os.environ.copy()
                    child_env["PYTHONIOENCODING"] = "utf-8"

                    if rscript_path:
                        result = subprocess.run(
                            [rscript_path, "outbreaker2/run_outbreaker2.R"],
                            stdout=lf,
                            stderr=subprocess.STDOUT,
                            cwd=project_root,
                            timeout=300,
                        )
                        if result.returncode != 0:
                            if allow_mock_fallback:
                                use_mock_fallback = True
                                lf.write("\n--- R execution failed, using mock report generator ---\n")
                            else:
                                raise Exception("R outbreaker2 execution failed and mock fallback is disabled")
                    else:
                        if allow_mock_fallback:
                            use_mock_fallback = True
                            lf.write("\n--- Rscript not found, using mock report generator ---\n")
                        else:
                            raise Exception("Rscript not found and mock fallback is disabled")

                    if use_mock_fallback:
                        mock_result = subprocess.run(
                            [python_exe, "scripts/generate_mock_outbreaker.py"],
                            stdout=lf,
                            stderr=subprocess.STDOUT,
                            cwd=project_root,
                            timeout=60,
                            env=child_env,
                        )
                        if mock_result.returncode != 0:
                            raise Exception("Both R and mock generator failed")
                    
                    # Generate supplementary visualizations without overwriting outbreaker network output.
                    lf.write("\n--- Generating supplementary visualizations ---\n")
                    if use_mock_fallback:
                        child_env["TB_SKIP_PRIORITY_NETWORK"] = "0"
                    else:
                        child_env["TB_SKIP_PRIORITY_NETWORK"] = "1"
                    priority_result = subprocess.run(
                        [python_exe, "scripts/generate_priority_visualizations.py"],
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                        cwd=project_root,
                        timeout=60,
                        env=child_env,
                    )
                    if priority_result.returncode != 0:
                        lf.write("Warning: Supplementary visualizations generation had issues\n")
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
