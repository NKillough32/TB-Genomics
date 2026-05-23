#!/usr/bin/env python3
"""Run optional advanced analysis tools against the active FASTA alignment.

The job is deliberately tolerant of missing external tools. It records which
tools are available, runs the installed tools, and writes a summary artifact for
workflow status and report provenance.
"""

from __future__ import annotations

import csv
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from scripts.runtime_paths import EXPORTS, UPLOADS
except ModuleNotFoundError:
    from runtime_paths import EXPORTS, UPLOADS

from backend.database import SessionLocal

OUT_DIR = EXPORTS / "fasta_analysis"
OUT_JSON = EXPORTS / "fasta_analysis_summary.json"

TOOL_CANDIDATES = {
    "seqkit": ["seqkit", "seqkit.exe"],
    "snp_sites": ["snp-sites", "snp-sites.exe"],
    "snp_dists": ["snp-dists", "snp-dists.exe"],
    "iqtree": ["iqtree3", "iqtree3.exe", "iqtree2", "iqtree2.exe", "iqtree", "iqtree.exe"],
    "treetime": ["treetime", "treetime.exe"],
}

WSL_TOOL_COMMANDS = {
    "seqkit": ["seqkit"],
    "snp_sites": ["snp-sites"],
    "snp_dists": ["snp-dists"],
    "iqtree": ["iqtree3", "iqtree2", "iqtree"],
    "treetime": ["treetime"],
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _which_many(candidates: list[str]) -> str | None:
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found

    venv_dir = ROOT / ".venv" / "Scripts"
    for candidate in candidates:
        path = venv_dir / candidate
        if path.exists():
            return str(path)
    return None


def _select_primary_fasta() -> Path | None:
    explicit = os.getenv("FASTA_ANALYSIS_FASTA") or os.getenv("TB_FASTA_ANALYSIS_FASTA")
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None

    dna_export = EXPORTS / "dna.fasta"
    if dna_export.exists():
        return dna_export

    candidates = [
        *sorted(EXPORTS.glob("*.fasta"), key=lambda p: p.stat().st_mtime, reverse=True),
        *sorted(UPLOADS.glob("*.fasta"), key=lambda p: p.stat().st_mtime, reverse=True),
        *sorted(UPLOADS.glob("*.fa"), key=lambda p: p.stat().st_mtime, reverse=True),
    ]
    return candidates[0] if candidates else None


def _read_fasta_headers(path: Path) -> list[str]:
    headers: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith(">"):
                headers.append(line[1:].strip().split()[0])
    return headers


def _probe_wsl() -> dict[str, Any]:
    if os.name != "nt":
        return {"available": False, "status": "skipped", "output": "non-windows host"}
    try:
        proc = subprocess.run(
            ["wsl", "--status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:
        return {"available": False, "status": "error", "output": f"wsl probe failed: {exc}"}

    output = (proc.stdout or "").replace("\x00", "").strip()
    first_line = output.splitlines()[0][:200] if output.splitlines() else f"exit_code={proc.returncode}"
    return {
        "available": proc.returncode == 0,
        "status": "ok" if proc.returncode == 0 else "error",
        "output": first_line,
    }


def _build_wsl_micromamba_command(wsl_env: str, args: list[str]) -> str:
    env_quoted = shlex.quote(wsl_env)
    args_quoted = " ".join(shlex.quote(arg) for arg in args)
    return (
        "source ~/.bashrc >/dev/null 2>&1 || true; "
        "if command -v micromamba >/dev/null 2>&1; then "
        f"micromamba run -n {env_quoted} {args_quoted}; "
        "elif [ -x ~/micromamba ]; then "
        f"~/micromamba run -n {env_quoted} {args_quoted}; "
        "else echo 'micromamba not found in WSL PATH or ~/micromamba'; exit 127; fi"
    )


def _win_to_wsl_path(path: Path) -> str | None:
    absolute = str(path.resolve()).replace("\\", "/")
    if len(absolute) >= 3 and absolute[1] == ":" and absolute[2] == "/":
        return f"/mnt/{absolute[0].lower()}{absolute[2:]}"
    if absolute.startswith("/"):
        return absolute
    return None


def _wsl_command_available(command_name: str, wsl_env: str) -> bool:
    probe = _build_wsl_micromamba_command(wsl_env, ["bash", "-lc", f"command -v {shlex.quote(command_name)}"])
    try:
        proc = subprocess.run(
            ["wsl", "--", "bash", "-lc", probe],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return False
    return proc.returncode == 0


def _select_wsl_command(command_names: list[str], wsl_env: str, wsl_available: bool) -> str | None:
    if not wsl_available:
        return None
    for command_name in command_names:
        if _wsl_command_available(command_name, wsl_env):
            return command_name
    return None


def _run_command(
    key: str,
    cmd: list[str],
    *,
    stdout_path: Path | None = None,
    timeout: int = 1800,
) -> dict[str, Any]:
    started = _iso_now()
    try:
        stdout_handle = stdout_path.open("w", encoding="utf-8") if stdout_path else subprocess.PIPE
        try:
            proc = subprocess.run(
                cmd,
                stdout=stdout_handle,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        finally:
            if stdout_path and hasattr(stdout_handle, "close"):
                stdout_handle.close()
    except subprocess.TimeoutExpired as exc:
        return {
            "key": key,
            "status": "timeout",
            "started_at": started,
            "finished_at": _iso_now(),
            "command": cmd,
            "message": f"Timed out after {timeout} seconds.",
            "stderr": str(exc)[:4000],
        }
    except Exception as exc:
        return {
            "key": key,
            "status": "error",
            "started_at": started,
            "finished_at": _iso_now(),
            "command": cmd,
            "message": str(exc),
        }

    stderr = (proc.stderr or "").strip()
    return {
        "key": key,
        "status": "completed" if proc.returncode == 0 else "failed",
        "started_at": started,
        "finished_at": _iso_now(),
        "returncode": proc.returncode,
        "command": cmd,
        "stdout": str(stdout_path) if stdout_path else "",
        "stderr": stderr[-4000:],
    }


def _run_wsl_command(
    key: str,
    wsl_env: str,
    args: list[str],
    *,
    stdout_path: Path | None = None,
    timeout: int = 1800,
) -> dict[str, Any]:
    started = _iso_now()
    bash_cmd = _build_wsl_micromamba_command(wsl_env, args)
    cmd = ["wsl", "--", "bash", "-lc", bash_cmd]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "key": key,
            "runner": "wsl",
            "status": "timeout",
            "started_at": started,
            "finished_at": _iso_now(),
            "command": cmd,
            "message": f"Timed out after {timeout} seconds.",
            "stderr": str(exc)[:4000],
        }
    except Exception as exc:
        return {
            "key": key,
            "runner": "wsl",
            "status": "error",
            "started_at": started,
            "finished_at": _iso_now(),
            "command": cmd,
            "message": str(exc),
        }

    if stdout_path:
        stdout_path.write_text(proc.stdout or "", encoding="utf-8")
    stderr = (proc.stderr or "").strip()
    return {
        "key": key,
        "runner": "wsl",
        "wsl_env": wsl_env,
        "status": "completed" if proc.returncode == 0 else "failed",
        "started_at": started,
        "finished_at": _iso_now(),
        "returncode": proc.returncode,
        "command": cmd,
        "stdout": str(stdout_path) if stdout_path else (proc.stdout or "")[-4000:],
        "stderr": stderr[-4000:],
    }


def _run_tool(
    key: str,
    tool: dict[str, Any],
    local_args: list[str],
    wsl_args: list[str],
    *,
    stdout_path: Path | None = None,
    timeout: int = 1800,
) -> dict[str, Any]:
    if tool.get("executable"):
        return _run_command(
            key,
            [str(tool["executable"]), *local_args],
            stdout_path=stdout_path,
            timeout=timeout,
        )
    if tool.get("wsl_command"):
        return _run_wsl_command(
            key,
            str(tool.get("wsl_env") or "tbtools"),
            [str(tool["wsl_command"]), *wsl_args],
            stdout_path=stdout_path,
            timeout=timeout,
        )
    return {
        "key": key,
        "status": "skipped",
        "message": "Tool is not available locally or through WSL fallback.",
    }


def _write_dates_file(fasta_headers: list[str], path: Path) -> dict[str, Any]:
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
                SELECT pseudonymised_case_id::text AS case_id, specimen_date
                FROM cases
                WHERE pseudonymised_case_id::text = ANY(:case_ids)
                """
            ),
            {"case_ids": fasta_headers},
        ).mappings().all()
    finally:
        db.close()

    date_by_case = {}
    for row in rows:
        value = row["specimen_date"]
        if isinstance(value, date):
            date_by_case[row["case_id"]] = value.isoformat()
        elif value:
            date_by_case[row["case_id"]] = str(value)[:10]

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["name", "date"])
        for case_id in fasta_headers:
            if case_id in date_by_case:
                writer.writerow([case_id, date_by_case[case_id]])

    return {"path": str(path), "dated_samples": len(date_by_case), "total_fasta_samples": len(fasta_headers)}


def _tool_records() -> dict[str, dict[str, Any]]:
    wsl_env = os.getenv("FASTA_ANALYSIS_WSL_ENV") or os.getenv("TBPROFILER_WSL_ENV", "tbtools")
    wsl_enabled = os.getenv("FASTA_ANALYSIS_WSL_FALLBACK", os.getenv("TBPROFILER_WSL_FALLBACK", "1")) == "1"
    wsl_probe = _probe_wsl() if wsl_enabled else {"available": False, "status": "disabled", "output": "WSL fallback disabled"}
    records = {}
    for key, candidates in TOOL_CANDIDATES.items():
        executable = _which_many(candidates)
        wsl_command = None
        if not executable:
            wsl_command = _select_wsl_command(WSL_TOOL_COMMANDS[key], wsl_env, bool(wsl_probe.get("available")))
        records[key] = {
            "key": key,
            "status": "available" if (executable or wsl_command) else "missing",
            "executable": executable,
            "runner": "local" if executable else ("wsl" if wsl_command else None),
            "wsl_command": wsl_command,
            "wsl_env": wsl_env,
            "candidates": candidates,
        }
    records["_wsl"] = {
        "status": wsl_probe.get("status"),
        "available": bool(wsl_probe.get("available")),
        "fallback_enabled": wsl_enabled,
        "env": wsl_env,
        "output": wsl_probe.get("output"),
    }
    return records


def main() -> None:
    EXPORTS.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    fasta = _select_primary_fasta()
    tools = _tool_records()
    summary: dict[str, Any] = {
        "status": "pending",
        "generated_at": _iso_now(),
        "input_fasta": str(fasta) if fasta else None,
        "tools": tools,
        "runs": {},
        "outputs": {},
    }

    if not fasta:
        summary["status"] = "missing_input"
        summary["message"] = "No FASTA file found. Generate outbreaker2 inputs or set FASTA_ANALYSIS_FASTA."
        OUT_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
        return

    headers = _read_fasta_headers(fasta)
    fasta_wsl = _win_to_wsl_path(fasta)
    out_dir_wsl = _win_to_wsl_path(OUT_DIR)
    summary["input"] = {
        "sample_count": len(headers),
        "fasta_bytes": fasta.stat().st_size,
    }

    if tools["seqkit"]["status"] == "available":
        out = OUT_DIR / "seqkit_stats.tsv"
        summary["runs"]["seqkit"] = _run_tool(
            "seqkit",
            tools["seqkit"],
            ["stats", "-T", str(fasta)],
            ["stats", "-T", fasta_wsl or str(fasta)],
            stdout_path=out,
            timeout=300,
        )
        summary["outputs"]["seqkit_stats"] = str(out)

    if tools["snp_sites"]["status"] == "available":
        out = OUT_DIR / "snp_sites.fasta"
        out_wsl = f"{out_dir_wsl}/snp_sites.fasta" if out_dir_wsl else str(out)
        summary["runs"]["snp_sites"] = _run_tool(
            "snp_sites",
            tools["snp_sites"],
            ["-o", str(out), str(fasta)],
            ["-o", out_wsl, fasta_wsl or str(fasta)],
            timeout=900,
        )
        summary["outputs"]["snp_sites_fasta"] = str(out)

    if tools["snp_dists"]["status"] == "available":
        out = OUT_DIR / "snp_distance_matrix.tsv"
        summary["runs"]["snp_dists"] = _run_tool(
            "snp_dists",
            tools["snp_dists"],
            [str(fasta)],
            [fasta_wsl or str(fasta)],
            stdout_path=out,
            timeout=900,
        )
        summary["outputs"]["snp_distance_matrix"] = str(out)

    treefile = OUT_DIR / "iqtree.treefile"
    if tools["iqtree"]["status"] == "available":
        prefix = OUT_DIR / "iqtree"
        prefix_wsl = f"{out_dir_wsl}/iqtree" if out_dir_wsl else str(prefix)
        summary["runs"]["iqtree"] = _run_tool(
            "iqtree",
            tools["iqtree"],
            [
                "-s",
                str(fasta),
                "-m",
                os.getenv("FASTA_ANALYSIS_IQTREE_MODEL", "GTR+G"),
                "-T",
                os.getenv("FASTA_ANALYSIS_THREADS", "AUTO"),
                "--prefix",
                str(prefix),
                "--redo",
            ],
            [
                "-s",
                fasta_wsl or str(fasta),
                "-m",
                os.getenv("FASTA_ANALYSIS_IQTREE_MODEL", "GTR+G"),
                "-T",
                os.getenv("FASTA_ANALYSIS_THREADS", "AUTO"),
                "--prefix",
                prefix_wsl,
                "--redo",
            ],
            timeout=int(os.getenv("FASTA_ANALYSIS_IQTREE_TIMEOUT", "3600")),
        )
        summary["outputs"]["iqtree_prefix"] = str(prefix)
        summary["outputs"]["iqtree_treefile"] = str(treefile)

    if tools["treetime"]["status"] == "available" and treefile.exists():
        dates_path = OUT_DIR / "sample_dates.tsv"
        summary["outputs"]["sample_dates"] = _write_dates_file(headers, dates_path)
        dates_path_wsl = f"{out_dir_wsl}/sample_dates.tsv" if out_dir_wsl else str(dates_path)
        treefile_wsl = f"{out_dir_wsl}/iqtree.treefile" if out_dir_wsl else str(treefile)
        treetime_out = OUT_DIR / "treetime"
        treetime_out.mkdir(parents=True, exist_ok=True)
        treetime_out_wsl = f"{out_dir_wsl}/treetime" if out_dir_wsl else str(treetime_out)
        summary["runs"]["treetime"] = _run_tool(
            "treetime",
            tools["treetime"],
            [
                "--aln",
                str(fasta),
                "--tree",
                str(treefile),
                "--dates",
                str(dates_path),
                "--outdir",
                str(treetime_out),
            ],
            [
                "--aln",
                fasta_wsl or str(fasta),
                "--tree",
                treefile_wsl,
                "--dates",
                dates_path_wsl,
                "--outdir",
                treetime_out_wsl,
            ],
            timeout=int(os.getenv("FASTA_ANALYSIS_TREETIME_TIMEOUT", "3600")),
        )
        summary["outputs"]["treetime_dir"] = str(treetime_out)
    elif tools["treetime"]["status"] == "available":
        summary["runs"]["treetime"] = {
            "key": "treetime",
            "status": "skipped",
            "message": "IQ-TREE treefile is required before TreeTime can run.",
        }

    completed = [run for run in summary["runs"].values() if run.get("status") == "completed"]
    failed = [run for run in summary["runs"].values() if run.get("status") in {"failed", "error", "timeout"}]
    if completed and not failed:
        summary["status"] = "completed"
    elif completed and failed:
        summary["status"] = "partial"
    elif summary["runs"]:
        summary["status"] = "failed"
    else:
        summary["status"] = "no_tools_available"
        summary["message"] = "No advanced FASTA analysis tools were found locally or through WSL fallback."

    OUT_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
