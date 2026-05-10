
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
                    child_env["R_LIBS_USER"] = os.path.join(project_root, "R_libs")

                    if rscript_path:
                        result = subprocess.run(
                            [rscript_path, "outbreaker2/run_outbreaker2.R"],
                            stdout=lf,
                            stderr=subprocess.STDOUT,
                            cwd=project_root,
                            timeout=300,
                            env=child_env,
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

                    # Always render a deterministic transmission tree graphic
                    # from the JSON network artifact so reports include a
                    # non-blank image even when R plotting backends differ.
                    tree_render_result = subprocess.run(
                        [python_exe, "scripts/render_transmission_tree.py"],
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                        cwd=project_root,
                        timeout=60,
                        env=child_env,
                    )
                    if tree_render_result.returncode != 0:
                        lf.write("\n--- Warning: transmission tree renderer failed ---\n")
                    
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


# Ordered steps for the full pipeline
PIPELINE_STEPS = [
    "run_lineage_dr_validation",
    "derive_sequence_clusters",
    "export_outbreaker",
    "run_outbreaker2",
    "compare_clustering_methods",
]


def run_pipeline():
    """Run all analysis steps sequentially under a single pipeline job ID."""
    pipeline_id = str(uuid.uuid4())
    log = f"logs/{pipeline_id}.log"
    JOBS[pipeline_id] = {
        "job": "full_pipeline",
        "status": "running",
        "progress": 0,
        "logfile": log,
        "pipeline_step": 0,
        "pipeline_total": len(PIPELINE_STEPS),
    }

    def _task():
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _log_to_audit("pipeline_started", "system", {"pipeline_id": pipeline_id, "steps": PIPELINE_STEPS})

        with open(log, "w", encoding="utf-8") as lf:
            total = len(PIPELINE_STEPS)
            for idx, step in enumerate(PIPELINE_STEPS):
                JOBS[pipeline_id]["pipeline_step"] = idx + 1
                JOBS[pipeline_id]["progress"] = int((idx / total) * 95)
                lf.write(f"\n{'='*60}\nPIPELINE STEP {idx+1}/{total}: {step}\n{'='*60}\n")
                lf.flush()

                # Re-use existing run_job logic by launching the step as a child job
                # and blocking until it finishes.
                child_id = run_job(step)
                if child_id is None:
                    JOBS[pipeline_id]["status"] = "failed"
                    lf.write(f"Step {step} is not allowed — aborting pipeline.\n")
                    _log_to_audit("pipeline_failed", "system", {"pipeline_id": pipeline_id, "failed_step": step})
                    return

                # Poll until child completes
                while True:
                    child = JOBS.get(child_id, {})
                    if child.get("status") in ("completed", "failed"):
                        break
                    threading.Event().wait(0.5)

                if JOBS.get(child_id, {}).get("status") == "failed":
                    JOBS[pipeline_id]["status"] = "failed"
                    lf.write(f"Step {step} failed — aborting pipeline.\n")
                    _log_to_audit("pipeline_failed", "system", {"pipeline_id": pipeline_id, "failed_step": step})
                    return

                lf.write(f"Step {step} completed OK.\n")

            JOBS[pipeline_id]["progress"] = 100
            JOBS[pipeline_id]["status"] = "completed"
            _log_to_audit("pipeline_completed", "system", {"pipeline_id": pipeline_id})

    threading.Thread(target=_task).start()
    return pipeline_id
