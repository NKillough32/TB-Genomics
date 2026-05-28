
import subprocess, uuid, threading, os
import sys
import json
import shutil
import glob
import traceback
import logging
from datetime import datetime
from sqlalchemy import text
from backend.database import SessionLocal
from backend.runtime_paths import ensure_runtime_dirs, log_path

ensure_runtime_dirs()

# Setup logging for job runner
logger = logging.getLogger(__name__)

# Ensure fallback env vars are set for lineage/DR tools (enables WSL/Docker fallback)
# These allow run_lineage_dr_validation to use alternative runners if local tools fail
_fallback_set = []
if os.getenv("TBPROFILER_WSL_FALLBACK") is None:
    os.environ["TBPROFILER_WSL_FALLBACK"] = "1"
    _fallback_set.append("TBPROFILER_WSL_FALLBACK=1")
if os.getenv("TBPROFILER_DOCKER_FALLBACK") is None:
    os.environ["TBPROFILER_DOCKER_FALLBACK"] = "1"
    _fallback_set.append("TBPROFILER_DOCKER_FALLBACK=1")
if os.getenv("TBPROFILER_WSL_ENV") is None:
    os.environ["TBPROFILER_WSL_ENV"] = "tbtools"
    _fallback_set.append("TBPROFILER_WSL_ENV=tbtools")

if _fallback_set:
    logger.debug(f"Job runner: Setting fallback env vars: {', '.join(_fallback_set)}")

# Use the current Python interpreter (venv)
python_exe = sys.executable

JOBS = {}
JOBS_LOCK = threading.RLock()
JOB_PROCESSES = {}


class JobCancelled(Exception):
    pass
ALLOWED_JOBS = {
    "audit_schema": [python_exe, "scripts/_audit_schema.py"],
    "derive_sequence_clusters": [python_exe, "scripts/derive_sequence_clusters.py"],
    "compare_clustering_methods": [python_exe, "scripts/compare_clustering_methods.py"],
    "run_clustering": [python_exe, "scripts/run_clustering.py"],
    "export_outbreaker": [python_exe, "scripts/export_outbreaker.py"],
    "run_lineage_dr_validation": [python_exe, "scripts/run_lineage_dr_validation.py"],
    "run_fasta_analysis": [python_exe, "scripts/run_fasta_analysis.py"],
    "generate_alerts": [python_exe, "scripts/generate_alerts.py"],
    "run_outbreaker2": ["Rscript", "outbreaker2/run_outbreaker2.R"],
    "run_transmission_synthesis": [python_exe, "scripts/run_transmission_synthesis.py"],
    "run_secondary_validation": [python_exe, "scripts/run_secondary_engine_validation.py"],
}


def set_job_state(job_id: str, **fields):
    with JOBS_LOCK:
        current = JOBS.get(job_id)
        if current is None:
            return
        fields.setdefault("updated_at", datetime.utcnow().isoformat() + "Z")
        current.update(fields)


def get_job_snapshot(job_id: str) -> dict | None:
    with JOBS_LOCK:
        current = JOBS.get(job_id)
        return dict(current) if isinstance(current, dict) else None


def _create_job(job_id: str, payload: dict):
    with JOBS_LOCK:
        now = datetime.utcnow().isoformat() + "Z"
        payload.setdefault("created_at", now)
        payload.setdefault("updated_at", now)
        payload.setdefault("cancel_requested", False)
        JOBS[job_id] = payload


def _is_cancel_requested(job_id: str) -> bool:
    with JOBS_LOCK:
        current = JOBS.get(job_id)
        return bool(isinstance(current, dict) and current.get("cancel_requested"))


def _terminate_process(job_id: str) -> bool:
    with JOBS_LOCK:
        process = JOB_PROCESSES.get(job_id)
    if process is None or process.poll() is not None:
        return False

    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            process.terminate()
        return True
    except Exception:
        try:
            process.kill()
            return True
        except Exception:
            return False


def cancel_job(job_id: str) -> dict:
    with JOBS_LOCK:
        current = JOBS.get(job_id)
        if current is None:
            return {"status": "unknown", "message": "Job not found"}
        if current.get("status") in {"completed", "failed", "cancelled"}:
            return {"status": current.get("status"), "message": "Job is no longer running"}
        current["cancel_requested"] = True
        current["status"] = "cancelling"
        current["updated_at"] = datetime.utcnow().isoformat() + "Z"
        active_child_id = current.get("active_child_id")

    terminated = _terminate_process(job_id)
    if active_child_id:
        cancel_job(str(active_child_id))
    return {"status": "cancelling", "terminated_process": terminated, "active_child_id": active_child_id}

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


def _write_log(lf, message: str):
    """Write timestamped message to log file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lf.write(f"[{timestamp}] {message}\n")
    lf.flush()


def _run_process(job_id: str, args: list[str], *, stdout, stderr, cwd: str, timeout: int | None = None, env: dict | None = None) -> int:
    started = datetime.utcnow()
    process = subprocess.Popen(args, stdout=stdout, stderr=stderr, cwd=cwd, env=env)
    with JOBS_LOCK:
        JOB_PROCESSES[job_id] = process
    try:
        while True:
            if _is_cancel_requested(job_id):
                _terminate_process(job_id)
                raise JobCancelled("Job cancelled by user")
            returncode = process.poll()
            if returncode is not None:
                return returncode
            if timeout is not None and (datetime.utcnow() - started).total_seconds() > timeout:
                _terminate_process(job_id)
                raise subprocess.TimeoutExpired(args, timeout)
            threading.Event().wait(0.5)
    finally:
        with JOBS_LOCK:
            JOB_PROCESSES.pop(job_id, None)


def _outbreaker_summary(project_root: str) -> dict:
    path = os.path.join(project_root, "exports", "outbreaker_summary.json")
    if os.getenv("TB_EXPORTS_DIR", "").strip():
        path = os.path.join(os.getenv("TB_EXPORTS_DIR", "").strip(), "outbreaker_summary.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _outbreaker_posterior_reliable(project_root: str) -> bool:
    return bool(_outbreaker_summary(project_root).get("posterior_reliable"))


def _run_outbreaker_r(job_id: str, rscript_path: str, project_root: str, lf, env: dict, timeout: int) -> subprocess.CompletedProcess:
    returncode = _run_process(
        job_id,
        [rscript_path, "outbreaker2/run_outbreaker2.R"],
        stdout=lf,
        stderr=subprocess.STDOUT,
        cwd=project_root,
        timeout=timeout,
        env=env,
    )
    return subprocess.CompletedProcess([rscript_path, "outbreaker2/run_outbreaker2.R"], returncode)


def run_job(job_name):
    if job_name not in ALLOWED_JOBS:
        return None
    job_id = str(uuid.uuid4())
    log = log_path(f"{job_id}.log")
    _create_job(job_id, {"job": job_name, "status": "queued", "progress": 0, "logfile": log})
    logger.debug(f"Job queued: {job_name} ({job_id})")

    def task():
        set_job_state(job_id, status="running", progress=10, started_at=datetime.utcnow().isoformat() + "Z")
        logger.debug(f"Job running: {job_name} ({job_id})")
        
        # Log job start
        _log_to_audit("job_started", "system", {"job_id": job_id, "job_name": job_name})
        
        with open(log, "w", encoding="utf-8") as lf:
            try:
                _write_log(lf, "=" * 70)
                _write_log(lf, f"JOB START: {job_name}")
                _write_log(lf, f"Job ID: {job_id}")
                _write_log(lf, "=" * 70)
                
                # Log environment and configuration
                _write_log(lf, f"Python interpreter: {python_exe}")
                _write_log(lf, f"Working directory: {os.getcwd()}")
                _write_log(lf, f"DATABASE_URL: {os.getenv('DATABASE_URL', '(not set)')[:50]}...")
                
                set_job_state(job_id, progress=40)
                # Get the project root (parent of backend dir)
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                _write_log(lf, f"Project root: {project_root}")
                
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
                    outbreaker_timeout = int(os.getenv("TB_OUTBREAKER_TIMEOUT_SEC", "1200"))
                    child_env = os.environ.copy()
                    child_env["PYTHONIOENCODING"] = "utf-8"
                    child_env["R_LIBS_USER"] = os.path.join(project_root, "R_libs")

                    if rscript_path:
                        result = None
                        try:
                            result = _run_outbreaker_r(job_id, rscript_path, project_root, lf, child_env, outbreaker_timeout)
                        except subprocess.TimeoutExpired:
                            if allow_mock_fallback:
                                use_mock_fallback = True
                                _write_log(lf, f"R execution timed out after {outbreaker_timeout}s, falling back to mock report generator")
                            else:
                                raise Exception(
                                    f"R outbreaker2 execution timed out after {outbreaker_timeout}s and mock fallback is disabled"
                                )
                        if result is not None and result.returncode != 0:
                            if allow_mock_fallback:
                                use_mock_fallback = True
                                _write_log(lf, f"R execution failed with code {result.returncode}, falling back to mock report generator")
                            else:
                                raise Exception(f"R outbreaker2 execution failed with code {result.returncode} and mock fallback is disabled")
                        auto_extend = os.getenv("TB_OUTBREAKER_AUTO_EXTEND", "1") == "1"
                        if result is not None and result.returncode == 0 and auto_extend and not _outbreaker_posterior_reliable(project_root):
                            summary = _outbreaker_summary(project_root)
                            _write_log(
                                lf,
                                "Posterior reliability thresholds not met after initial outbreaker2 run: "
                                + ", ".join(str(x) for x in summary.get("posterior_reliability_reasons", [])),
                            )
                            extended_env = child_env.copy()
                            extended_env["TB_OUTBREAKER_ITER"] = os.getenv("TB_OUTBREAKER_RELIABLE_ITER", "250000")
                            extended_env["TB_OUTBREAKER_BURNIN"] = os.getenv("TB_OUTBREAKER_RELIABLE_BURNIN", "50000")
                            extended_env["TB_OUTBREAKER_THIN"] = os.getenv("TB_OUTBREAKER_RELIABLE_THIN", extended_env.get("TB_OUTBREAKER_THIN", "10"))
                            _write_log(
                                lf,
                                "Auto-extending outbreaker2 run for posterior reliability: "
                                f"iter={extended_env['TB_OUTBREAKER_ITER']}, "
                                f"burnin={extended_env['TB_OUTBREAKER_BURNIN']}, "
                                f"thin={extended_env['TB_OUTBREAKER_THIN']}",
                            )
                            result = _run_outbreaker_r(job_id, rscript_path, project_root, lf, extended_env, outbreaker_timeout)
                            if result.returncode != 0:
                                raise Exception(f"Extended R outbreaker2 execution failed with code {result.returncode}")
                    else:
                        if allow_mock_fallback:
                            use_mock_fallback = True
                            _write_log(lf, "Rscript not found, falling back to mock report generator")
                        else:
                            raise Exception("Rscript not found and mock fallback is disabled")

                    if use_mock_fallback:
                        _write_log(lf, "Running mock Outbreaker2 generator...")
                        mock_returncode = _run_process(
                            job_id,
                            [python_exe, "scripts/generate_mock_outbreaker.py"],
                            stdout=lf,
                            stderr=subprocess.STDOUT,
                            cwd=project_root,
                            timeout=60,
                            env=child_env,
                        )
                        mock_result = subprocess.CompletedProcess([python_exe, "scripts/generate_mock_outbreaker.py"], mock_returncode)
                        if mock_result.returncode != 0:
                            raise Exception(f"Mock generator failed with code {mock_result.returncode}")

                    # Always render a deterministic transmission tree graphic
                    # from the JSON network artifact so reports include a
                    # non-blank image even when R plotting backends differ.
                    _write_log(lf, "Rendering transmission tree graphic...")
                    tree_render_returncode = _run_process(
                        job_id,
                        [python_exe, "scripts/render_transmission_tree.py"],
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                        cwd=project_root,
                        timeout=60,
                        env=child_env,
                    )
                    tree_render_result = subprocess.CompletedProcess([python_exe, "scripts/render_transmission_tree.py"], tree_render_returncode)
                    if tree_render_result.returncode != 0:
                        _write_log(lf, f"Warning: Transmission tree renderer exited with code {tree_render_result.returncode}")
                    
                    # Generate supplementary visualizations without overwriting outbreaker network output.
                    _write_log(lf, "Generating supplementary visualizations...")
                    if use_mock_fallback:
                        child_env["TB_SKIP_PRIORITY_NETWORK"] = "0"
                    else:
                        child_env["TB_SKIP_PRIORITY_NETWORK"] = "1"
                    priority_returncode = _run_process(
                        job_id,
                        [python_exe, "scripts/generate_priority_visualizations.py"],
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                        cwd=project_root,
                        timeout=60,
                        env=child_env,
                    )
                    priority_result = subprocess.CompletedProcess([python_exe, "scripts/generate_priority_visualizations.py"], priority_returncode)
                    if priority_result.returncode != 0:
                        _write_log(lf, f"Warning: Supplementary visualizations exited with code {priority_result.returncode}")
                else:
                    _write_log(lf, f"Running job: {' '.join(ALLOWED_JOBS[job_name])}")
                    returncode = _run_process(
                        job_id,
                        ALLOWED_JOBS[job_name],
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                        cwd=project_root,
                    )
                    result = subprocess.CompletedProcess(ALLOWED_JOBS[job_name], returncode)
                    if result.returncode != 0:
                        raise Exception(f"{job_name} exited with code {result.returncode}")
                
                _write_log(lf, "=" * 70)
                _write_log(lf, f"JOB COMPLETED SUCCESSFULLY")
                _write_log(lf, "=" * 70)
                set_job_state(job_id, progress=100, status="completed", finished_at=datetime.utcnow().isoformat() + "Z")
                logger.info(f"Job completed: {job_name} ({job_id})")
                
                # Log job completion
                _log_to_audit("job_completed", "system", {"job_id": job_id, "job_name": job_name})
            except JobCancelled as e:
                set_job_state(job_id, status="cancelled", finished_at=datetime.utcnow().isoformat() + "Z")
                _write_log(lf, "=" * 70)
                _write_log(lf, "JOB CANCELLED")
                _write_log(lf, "=" * 70)
                _write_log(lf, str(e))
                logger.info(f"Job cancelled: {job_name} ({job_id})")
                _log_to_audit("job_cancelled", "system", {"job_id": job_id, "job_name": job_name})
            except Exception as e:
                set_job_state(job_id, status="failed", finished_at=datetime.utcnow().isoformat() + "Z")
                _write_log(lf, "=" * 70)
                _write_log(lf, "JOB FAILED")
                _write_log(lf, "=" * 70)
                _write_log(lf, f"Error: {str(e)}")
                _write_log(lf, "\nFull traceback:")
                lf.write(traceback.format_exc())
                lf.flush()
                logger.error(f"Job failed: {job_name} ({job_id}) - {str(e)}")
                
                # Log job failure
                _log_to_audit("job_failed", "system", {"job_id": job_id, "job_name": job_name, "error": str(e)})

    threading.Thread(target=task).start()
    return job_id


# Ordered steps for the full pipeline
PIPELINE_STEPS = [
    "derive_sequence_clusters",
    "export_outbreaker",
    "run_fasta_analysis",
    "run_lineage_dr_validation",
    "generate_alerts",
    "run_outbreaker2",
    "compare_clustering_methods",
    "run_transmission_synthesis",
]


def _legacy_run_pipeline():
    """Run all analysis steps sequentially under a single pipeline job ID."""
    pipeline_id = str(uuid.uuid4())
    log = log_path(f"{pipeline_id}.log")
    _create_job(pipeline_id, {
        "job": "full_pipeline",
        "status": "running",
        "progress": 0,
        "logfile": log,
        "pipeline_step": 0,
        "pipeline_total": len(PIPELINE_STEPS),
        "completed_steps": 0,
        "current_step": None,
        "active_child_id": None,
    })

    def _task():
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _log_to_audit("pipeline_started", "system", {"pipeline_id": pipeline_id, "steps": PIPELINE_STEPS})

        with open(log, "w", encoding="utf-8") as lf:
            _write_log(lf, "=" * 70)
            _write_log(lf, "PIPELINE START")
            _write_log(lf, f"Pipeline ID: {pipeline_id}")
            _write_log(lf, f"Total steps: {len(PIPELINE_STEPS)}")
            _write_log(lf, f"Steps: {', '.join(PIPELINE_STEPS)}")
            _write_log(lf, "=" * 70)
            
            total = len(PIPELINE_STEPS)
            for idx, step in enumerate(PIPELINE_STEPS):
                set_job_state(
                    pipeline_id,
                    pipeline_step=idx + 1,
                    progress=int((idx / total) * 95),
                )
                _write_log(lf, f"\nPIPELINE STEP {idx+1}/{total}: {step}")
                _write_log(lf, "-" * 70)

                # Re-use existing run_job logic by launching the step as a child job
                # and blocking until it finishes.
                child_id = run_job(step)
                if child_id is None:
                    set_job_state(pipeline_id, status="failed")
                    _write_log(lf, f"ERROR: Step {step} is not allowed - aborting pipeline")
                    _log_to_audit("pipeline_failed", "system", {"pipeline_id": pipeline_id, "failed_step": step})
                    return

                _write_log(lf, f"Child job ID: {child_id}")

                # Poll until child completes, propagating child progress into the
                # pipeline's own slice of the 0–95% range so the bar moves smoothly.
                step_start = int((idx / total) * 95)
                step_end = int(((idx + 1) / total) * 95)
                step_width = step_end - step_start
                while True:
                    child = get_job_snapshot(child_id) or {}
                    if child.get("status") in ("completed", "failed"):
                        break
                    child_pct = child.get("progress", 0) or 0
                    pipeline_pct = step_start + int((child_pct / 100) * step_width)
                    set_job_state(pipeline_id, progress=pipeline_pct)
                    threading.Event().wait(0.5)

                child_status = (get_job_snapshot(child_id) or {}).get("status")
                child_progress = (get_job_snapshot(child_id) or {}).get("progress", "?")
                _write_log(lf, f"Step {step} finished with status: {child_status} (progress: {child_progress}%)")
                
                if child_status == "failed":
                    set_job_state(pipeline_id, status="failed")
                    _write_log(lf, f"ERROR: Step {step} failed - aborting pipeline")
                    _log_to_audit("pipeline_failed", "system", {"pipeline_id": pipeline_id, "failed_step": step})
                    return

            _write_log(lf, "\n" + "=" * 70)
            _write_log(lf, "PIPELINE COMPLETED SUCCESSFULLY")
            _write_log(lf, "=" * 70)
            set_job_state(pipeline_id, progress=100, status="completed")
            _log_to_audit("pipeline_completed", "system", {"pipeline_id": pipeline_id})

    threading.Thread(target=_task).start()
    return pipeline_id


def run_pipeline():
    """Run all analysis steps sequentially under a single cancellable pipeline job."""
    pipeline_id = str(uuid.uuid4())
    log = log_path(f"{pipeline_id}.log")
    _create_job(pipeline_id, {
        "job": "full_pipeline",
        "status": "running",
        "progress": 0,
        "logfile": log,
        "pipeline_step": 0,
        "pipeline_total": len(PIPELINE_STEPS),
        "completed_steps": 0,
        "current_step": None,
        "active_child_id": None,
    })

    def _task():
        _log_to_audit("pipeline_started", "system", {"pipeline_id": pipeline_id, "steps": PIPELINE_STEPS})

        with open(log, "w", encoding="utf-8") as lf:
            try:
                set_job_state(pipeline_id, started_at=datetime.utcnow().isoformat() + "Z")
                _write_log(lf, "=" * 70)
                _write_log(lf, "PIPELINE START")
                _write_log(lf, f"Pipeline ID: {pipeline_id}")
                _write_log(lf, f"Total steps: {len(PIPELINE_STEPS)}")
                _write_log(lf, f"Steps: {', '.join(PIPELINE_STEPS)}")
                _write_log(lf, "=" * 70)

                total = len(PIPELINE_STEPS)
                for idx, step in enumerate(PIPELINE_STEPS):
                    if _is_cancel_requested(pipeline_id):
                        raise JobCancelled("Pipeline cancelled by user")
                    set_job_state(
                        pipeline_id,
                        status="running",
                        pipeline_step=idx + 1,
                        completed_steps=idx,
                        current_step=step,
                        progress=int((idx / total) * 95),
                    )
                    _write_log(lf, f"\nPIPELINE STEP {idx + 1}/{total}: {step}")
                    _write_log(lf, "-" * 70)

                    child_id = run_job(step)
                    if child_id is None:
                        set_job_state(pipeline_id, status="failed")
                        _write_log(lf, f"ERROR: Step {step} is not allowed - aborting pipeline")
                        _log_to_audit("pipeline_failed", "system", {"pipeline_id": pipeline_id, "failed_step": step})
                        return

                    set_job_state(pipeline_id, active_child_id=child_id)
                    _write_log(lf, f"Child job ID: {child_id}")

                    step_start = int((idx / total) * 95)
                    step_end = int(((idx + 1) / total) * 95)
                    step_width = step_end - step_start
                    while True:
                        if _is_cancel_requested(pipeline_id):
                            cancel_job(child_id)
                            raise JobCancelled("Pipeline cancelled by user")
                        child = get_job_snapshot(child_id) or {}
                        if child.get("status") in ("completed", "failed", "cancelled"):
                            break
                        child_pct = child.get("progress", 0) or 0
                        pipeline_pct = step_start + int((child_pct / 100) * step_width)
                        set_job_state(
                            pipeline_id,
                            progress=pipeline_pct,
                            child_progress=child_pct,
                            child_status=child.get("status"),
                        )
                        threading.Event().wait(0.5)

                    child_snapshot = get_job_snapshot(child_id) or {}
                    child_status = child_snapshot.get("status")
                    child_progress = child_snapshot.get("progress", "?")
                    set_job_state(pipeline_id, child_progress=child_progress, child_status=child_status)
                    _write_log(lf, f"Step {step} finished with status: {child_status} (progress: {child_progress}%)")

                    if child_status == "cancelled":
                        raise JobCancelled("Pipeline cancelled by user")
                    if child_status == "failed":
                        set_job_state(pipeline_id, status="failed", active_child_id=None, finished_at=datetime.utcnow().isoformat() + "Z")
                        _write_log(lf, f"ERROR: Step {step} failed - aborting pipeline")
                        _log_to_audit("pipeline_failed", "system", {"pipeline_id": pipeline_id, "failed_step": step})
                        return

                _write_log(lf, "\n" + "=" * 70)
                _write_log(lf, "PIPELINE COMPLETED SUCCESSFULLY")
                _write_log(lf, "=" * 70)
                set_job_state(
                    pipeline_id,
                    progress=100,
                    status="completed",
                    completed_steps=total,
                    active_child_id=None,
                    child_progress=None,
                    child_status=None,
                    finished_at=datetime.utcnow().isoformat() + "Z",
                )
                _log_to_audit("pipeline_completed", "system", {"pipeline_id": pipeline_id})
            except JobCancelled as e:
                set_job_state(pipeline_id, status="cancelled", active_child_id=None, finished_at=datetime.utcnow().isoformat() + "Z")
                _write_log(lf, "\n" + "=" * 70)
                _write_log(lf, "PIPELINE CANCELLED")
                _write_log(lf, "=" * 70)
                _write_log(lf, str(e))
                _log_to_audit("pipeline_cancelled", "system", {"pipeline_id": pipeline_id})

    threading.Thread(target=_task).start()
    return pipeline_id
