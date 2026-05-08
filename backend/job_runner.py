
import subprocess, uuid, threading, os

os.makedirs("logs", exist_ok=True)

JOBS = {}
ALLOWED_JOBS = {
    "run_clustering": ["python", "scripts/run_clustering.py"],
    "export_outbreaker": ["python", "scripts/export_outbreaker.py"],
    "run_outbreaker2": ["Rscript", "outbreaker2/run_outbreaker2.R"],
}

def run_job(job_name):
    if job_name not in ALLOWED_JOBS:
        return None
    job_id = str(uuid.uuid4())
    log = f"logs/{job_id}.log"
    JOBS[job_id] = {"job": job_name, "status": "queued", "progress": 0, "logfile": log}

    def task():
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["progress"] = 10
        with open(log, "w") as lf:
            try:
                JOBS[job_id]["progress"] = 40
                subprocess.run(ALLOWED_JOBS[job_name], stdout=lf, stderr=lf, check=True)
                JOBS[job_id]["progress"] = 100
                JOBS[job_id]["status"] = "completed"
            except Exception as e:
                JOBS[job_id]["status"] = "failed"
                lf.write(str(e))

    threading.Thread(target=task).start()
    return job_id
