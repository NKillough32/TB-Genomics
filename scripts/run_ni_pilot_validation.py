#!/usr/bin/env python3
"""Run strict validation checks for the NI pilot package.

The runner is intentionally conservative:
- a dirty worktree blocks a formal pass unless --allow-dirty is supplied;
- the NI dataset directory is mandatory for pilot validation;
- command stdout/stderr is saved under validation/ni_pilot/runs/<run_id>/.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_ROOT = PROJECT_ROOT / "validation" / "ni_pilot" / "runs"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _run(command: list[str], cwd: Path, log_path: Path, env: dict[str, str] | None = None) -> dict:
    started = datetime.now(timezone.utc)
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    finished = datetime.now(timezone.utc)
    log_path.write_text(proc.stdout, encoding="utf-8")
    return {
        "command": command,
        "returncode": proc.returncode,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "log": str(log_path.relative_to(PROJECT_ROOT)),
        "status": "pass" if proc.returncode == 0 else "fail",
    }


def _git(args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(PROJECT_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _read_requirements(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _dataset_summary(dataset: Path) -> dict:
    required = [
        "cases.csv",
        "tb_interpretation.csv",
        "sequencing_runs.csv",
        "sample_qc_metrics.csv",
        "analysis_provenance.csv",
        "dna.fasta",
        "ingest_manifest.json",
    ]
    files = {}
    for name in required:
        path = dataset / name
        files[name] = {
            "present": path.exists(),
            "bytes": path.stat().st_size if path.exists() else None,
        }
    return {"path": str(dataset), "required_files": files}


def _write_markdown_report(report: dict, path: Path) -> None:
    checks = report["checks"]
    lines = [
        "# NI Pilot Validation Report",
        "",
        f"- Status: `{report['status']}`",
        f"- Run ID: `{report['run_id']}`",
        f"- Generated at: `{report['generated_at']}`",
        f"- Branch: `{report['code_freeze']['branch']}`",
        f"- Commit: `{report['code_freeze']['commit']}`",
        f"- Dataset: `{report['dataset']['path']}`",
        "",
        "## Decision",
        "",
        report["decision"],
        "",
        "## Checks",
        "",
        "| Check | Status | Evidence |",
        "| --- | --- | --- |",
    ]
    for check in checks:
        lines.append(f"| {check['id']} {check['name']} | {check['status']} | {check.get('evidence', '')} |")
    lines.extend(
        [
            "",
            "## Code Freeze",
            "",
            f"- Dirty worktree: `{report['code_freeze']['dirty']}`",
            f"- Dirty files: `{', '.join(report['code_freeze']['dirty_files']) or 'none'}`",
            "",
            "## Sign-Off",
            "",
            "This report is generated evidence. It is not signed until the roles below are completed.",
            "",
            "| Role | Name | Decision | Signature | Date |",
            "| --- | --- | --- | --- | --- |",
            "| Technical validation lead |  |  |  |  |",
            "| Public health / epidemiology reviewer |  |  |  |  |",
            "| Information governance reviewer |  |  |  |  |",
            "| Pilot owner / service lead |  |  |  |  |",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> dict:
    run_id = args.run_id or _utc_stamp()
    run_dir = RUN_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    dataset = Path(args.dataset).resolve()
    python_exe = sys.executable
    if (PROJECT_ROOT / ".venv" / "Scripts" / "python.exe").exists():
        python_exe = str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")

    dirty_output = _git(["status", "--short"])
    dirty_files = [line.strip() for line in dirty_output.splitlines() if line.strip()]
    code_freeze = {
        "branch": _git(["branch", "--show-current"]),
        "commit": _git(["rev-parse", "HEAD"]),
        "commit_short": _git(["rev-parse", "--short", "HEAD"]),
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files,
        "pyproject": (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        "backend_requirements": _read_requirements(PROJECT_ROOT / "backend" / "requirements.txt"),
        "dev_requirements": _read_requirements(PROJECT_ROOT / "requirements-dev.txt"),
    }

    checks: list[dict] = []
    if code_freeze["dirty"] and not args.allow_dirty:
        checks.append(
            {
                "id": "VP-001",
                "name": "Code freeze",
                "status": "fail",
                "evidence": "Worktree is dirty; rerun after commit/tag or use --allow-dirty for draft evidence.",
            }
        )
    else:
        checks.append(
            {
                "id": "VP-001",
                "name": "Code freeze",
                "status": "pass" if not code_freeze["dirty"] else "draft-only",
                "evidence": f"Commit {code_freeze['commit_short']} on {code_freeze['branch']}.",
            }
        )

    if not dataset.exists():
        checks.append(
            {
                "id": "VP-002",
                "name": "NI ingest bundle validation",
                "status": "fail",
                "evidence": "Representative pseudonymised NI dataset directory was not found.",
            }
        )
    else:
        result = _run(
            [python_exe, "scripts/validate_ingest_files.py", "--dir", str(dataset)],
            PROJECT_ROOT,
            run_dir / "vp002_ingest_validation.log",
        )
        checks.append(
            {
                "id": "VP-002",
                "name": "NI ingest bundle validation",
                "status": result["status"],
                "evidence": result["log"],
                "command_result": result,
            }
        )

    checks.append(
        {
            "id": "VP-002A",
            "name": "Representative NI dataset declaration",
            "status": "pass" if args.confirm_representative_ni_dataset else "fail",
            "evidence": (
                "Validation lead confirmed this is representative pseudonymised NI pilot data."
                if args.confirm_representative_ni_dataset
                else "Dataset has not been declared representative pseudonymised NI pilot data."
            ),
        }
    )

    wgs_result = _run(
        [python_exe, "validation/run_validation.py", "--observed", str(run_dir / "wgs_observed")],
        PROJECT_ROOT,
        run_dir / "vp003_wgs_regression.log",
    )
    checks.append(
        {
            "id": "VP-003",
            "name": "WGS contract regression",
            "status": wgs_result["status"],
            "evidence": wgs_result["log"],
            "command_result": wgs_result,
        }
    )

    pytest_result = _run(
        [python_exe, "-m", "pytest"],
        PROJECT_ROOT,
        run_dir / "vp004_pytest.log",
    )
    checks.append(
        {
            "id": "VP-004",
            "name": "Automated test suite",
            "status": pytest_result["status"],
            "evidence": pytest_result["log"],
            "command_result": pytest_result,
        }
    )

    env_flags = {
        "TB_ENABLE_SYNTHETIC_SEEDING": os.getenv("TB_ENABLE_SYNTHETIC_SEEDING"),
        "TB_ALLOW_NON_OPERATIONAL_ACTIONS": os.getenv("TB_ALLOW_NON_OPERATIONAL_ACTIONS"),
        "TB_AUTH_REQUIRED": os.getenv("TB_AUTH_REQUIRED"),
        "TB_AUTH_TOKENS_configured": bool(os.getenv("TB_AUTH_TOKENS")),
    }
    safety_pass = not env_flags["TB_ENABLE_SYNTHETIC_SEEDING"] and not env_flags["TB_ALLOW_NON_OPERATIONAL_ACTIONS"]
    checks.append(
        {
            "id": "VP-005",
            "name": "Operational safety flags",
            "status": "pass" if safety_pass else "fail",
            "evidence": json.dumps(env_flags, sort_keys=True),
        }
    )

    failed = [check for check in checks if check["status"] == "fail"]
    draft = [check for check in checks if check["status"] == "draft-only"]
    if failed:
        status = "blocked"
        decision = "Formal NI pilot validation is blocked until failed checks are remediated and the report is signed."
    elif draft:
        status = "draft-only"
        decision = "Validation evidence is draft-only because the code freeze was not clean."
    else:
        status = "awaiting-signature"
        decision = "Automated evidence passed. Formal pilot validation remains pending reviewer signatures."

    report = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "decision": decision,
        "dataset": _dataset_summary(dataset),
        "code_freeze": code_freeze,
        "checks": checks,
    }

    (run_dir / "validation_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _write_markdown_report(report, run_dir / "validation_report.md")
    if args.copy_latest:
        latest_dir = RUN_ROOT / "latest"
        if latest_dir.exists():
            shutil.rmtree(latest_dir)
        shutil.copytree(run_dir, latest_dir)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run strict NI pilot validation checks.")
    parser.add_argument("--dataset", required=True, help="Path to representative pseudonymised NI ingest bundle.")
    parser.add_argument(
        "--confirm-representative-ni-dataset",
        action="store_true",
        help="Required for formal pass: confirms the dataset is representative pseudonymised NI pilot data.",
    )
    parser.add_argument("--run-id", help="Optional stable run identifier.")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow draft evidence with a dirty worktree.")
    parser.add_argument("--copy-latest", action="store_true", help="Copy run outputs to validation/ni_pilot/runs/latest.")
    return parser.parse_args()


def main() -> None:
    report = run(parse_args())
    print(json.dumps({"run_id": report["run_id"], "status": report["status"], "decision": report["decision"]}, indent=2))
    if report["status"] in {"blocked", "draft-only"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
