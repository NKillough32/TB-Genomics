#!/usr/bin/env python3
"""Lineage and drug-resistance integration scaffold (TB-Profiler/Mykrobe).

This script does three things:
1) Detects whether common external tools are installed and callable.
2) Verifies input readiness (FASTA/FASTQ/VCF presence).
3) Optionally imports standardized lineage/DR calls from CSV into tb_interpretation.

The import CSV (optional) should include columns:
    sample_id,lineage,predicted_drug_resistance,confidence_score,interpretation_summary

`predicted_drug_resistance` may be JSON (object/array) or plain text.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
EXPORTS = ROOT / "exports"
UPLOADS = ROOT / "uploads"
OUT_JSON = EXPORTS / "lineage_dr_validation.json"

# Add project root to import path for direct script execution.
sys.path.insert(0, str(ROOT))

from backend.database import SessionLocal


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _which_many(candidates: list[str]) -> str | None:
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found

    # Fall back to local venv scripts when PATH has not been refreshed.
    venv_dir = ROOT / ".venv" / "Scripts"
    for name in candidates:
        local = venv_dir / name
        if local.exists():
            return str(local)
    return None


def _build_exec_command(exec_path: str, args: list[str]) -> list[str]:
    path_obj = Path(exec_path)
    name = path_obj.name.lower()
    # Entry-point scripts in .venv/Scripts are text wrappers on Windows and
    # should be invoked via the active interpreter.
    if path_obj.exists() and name in {"tb-profiler", "tb-profiler-tools"}:
        return [sys.executable, exec_path, *args]
    return [exec_path, *args]


def _probe_version(exec_path: str, args: list[str]) -> tuple[str, str]:
    try:
        cmd = _build_exec_command(exec_path, args)
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - defensive in integration script
        return "unknown", f"version probe failed: {exc}"

    output = (proc.stdout or "").strip()
    if not output:
        output = f"exit_code={proc.returncode}"
    lines = output.splitlines()
    first_line = lines[0][:200]
    last_line = lines[-1][:200]
    summary = first_line if first_line == last_line else f"{first_line} | {last_line}"
    if proc.returncode == 0:
        return "ok", summary
    return "error", summary


def _discover_inputs() -> dict[str, Any]:
    fastq = sorted(
        [
            *UPLOADS.glob("*.fastq"),
            *UPLOADS.glob("*.fastq.gz"),
            *UPLOADS.glob("*.fq"),
            *UPLOADS.glob("*.fq.gz"),
        ]
    )
    vcf = sorted([*UPLOADS.glob("*.vcf"), *UPLOADS.glob("*.vcf.gz")])
    fasta = sorted([*EXPORTS.glob("*.fasta"), *UPLOADS.glob("*.fasta"), *UPLOADS.glob("*.fa")])

    return {
        "fastq_count": len(fastq),
        "vcf_count": len(vcf),
        "fasta_count": len(fasta),
        "fastq_examples": [p.name for p in fastq[:5]],
        "vcf_examples": [p.name for p in vcf[:5]],
        "fasta_examples": [p.name for p in fasta[:5]],
    }


def _discover_fasta_inputs() -> list[Path]:
    return sorted([*EXPORTS.glob("*.fasta"), *UPLOADS.glob("*.fasta"), *UPLOADS.glob("*.fa")])


def _probe_docker() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["docker", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:
        return {
            "available": False,
            "status": "error",
            "output": f"docker probe failed: {exc}",
        }
    output = (proc.stdout or "").strip()
    if proc.returncode == 0:
        base = {
            "available": True,
            "status": "ok",
            "output": output.splitlines()[0][:200] if output else "docker available",
        }

        daemon_proc = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
            check=False,
        )
        daemon_output = (daemon_proc.stdout or "").strip()
        if daemon_proc.returncode == 0:
            base["daemon_running"] = True
            base["daemon_probe"] = "docker daemon reachable"
        else:
            base["daemon_running"] = False
            base["daemon_probe"] = daemon_output.splitlines()[0][:200] if daemon_output else f"exit_code={daemon_proc.returncode}"

        return base

    summary = output.splitlines()[0][:200] if output else f"exit_code={proc.returncode}"
    return {
        "available": False,
        "status": "error",
        "output": summary,
        "daemon_running": False,
        "daemon_probe": "docker cli unavailable",
    }


def _probe_wsl() -> dict[str, Any]:
    if os.name != "nt":
        return {
            "available": False,
            "status": "skipped",
            "output": "non-windows host",
        }

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
        return {
            "available": False,
            "status": "error",
            "output": f"wsl probe failed: {exc}",
        }

    output = (proc.stdout or "").replace("\x00", "").strip()
    lines = output.splitlines()
    summary = lines[0][:200] if lines else f"exit_code={proc.returncode}"
    if proc.returncode == 0:
        return {
            "available": True,
            "status": "ok",
            "output": summary,
            "details": lines[:10],
        }
    return {
        "available": False,
        "status": "error",
        "output": summary,
        "details": lines[:10],
    }


def _wsl_probe_tool(command: str) -> tuple[str, str]:
    try:
        proc = subprocess.run(
            ["wsl", "--", "bash", "-lc", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        return "unknown", f"wsl probe failed: {exc}"

    output = (proc.stdout or "").strip()
    if not output:
        output = f"exit_code={proc.returncode}"
    lines = output.splitlines()
    first_line = lines[0][:200]
    last_line = lines[-1][:200]
    summary = first_line if first_line == last_line else f"{first_line} | {last_line}"
    return ("ok", summary) if proc.returncode == 0 else ("error", summary)


def _win_to_wsl_path(path: Path) -> str | None:
    absolute = str(path.resolve()).replace("\\", "/")
    # Convert Windows paths like C:/Users/... into /mnt/c/Users/...
    if len(absolute) >= 3 and absolute[1] == ":" and absolute[2] == "/":
        drive = absolute[0].lower()
        tail = absolute[2:]
        return f"/mnt/{drive}{tail}"
    if absolute.startswith("/"):
        return absolute
    return None


def _to_container_path(path: Path) -> str:
    relative = path.resolve().relative_to(ROOT.resolve())
    return "/work/" + str(relative).replace("\\", "/")


def _split_fasta_records(path: Path, max_records: int = 10) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    header: str | None = None
    chunks: list[str] = []

    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith(">"):
                if header and chunks:
                    records.append((header, "".join(chunks)))
                    if len(records) >= max_records:
                        break
                header = line[1:].strip().split()[0]
                chunks = []
            elif header:
                chunks.append(line)

    if len(records) < max_records and header and chunks:
        records.append((header, "".join(chunks)))

    return records[:max_records]


def _extract_lineage_and_resistance(result_json: Path) -> dict[str, Any]:
    payload = json.loads(result_json.read_text(encoding="utf-8"))

    lineage: Any = None
    dr_payload: Any = None
    confidence = None
    summary = None

    if isinstance(payload, dict):
        lineage = (
            payload.get("lineage")
            or payload.get("main_lineage")
            or payload.get("lineage_name")
            or None
        )
        # Keep resistance structure broad because tool schema varies by db/release.
        dr_payload = (
            payload.get("dr_variants")
            or payload.get("dr")
            or payload.get("drug_resistance")
            or payload.get("resistance")
            or None
        )
        confidence = payload.get("lineage_confidence") or payload.get("confidence")
        summary = payload.get("resistance_summary") or payload.get("prediction") or None

    # tb-profiler lineage can be a list/dict structure. Persist a compact string
    # for the lineage column while preserving full details in JSON fields.
    lineage_value: str | None
    if isinstance(lineage, list):
        first = lineage[0] if lineage else None
        if isinstance(first, dict):
            lineage_value = str(first.get("lineage") or first.get("id") or "") or None
        else:
            lineage_value = str(first) if first is not None else None
    elif isinstance(lineage, dict):
        lineage_value = str(lineage.get("lineage") or lineage.get("id") or "") or None
    elif lineage is None:
        lineage_value = None
    else:
        lineage_value = str(lineage)

    return {
        "lineage": lineage_value,
        "predicted_drug_resistance": dr_payload,
        "confidence_score": _to_float(str(confidence)) if confidence is not None else None,
        "interpretation_summary": str(summary) if summary is not None else None,
    }


def _run_tbprofiler_on_fasta(tbprofiler_exec: str, fasta_files: list[Path]) -> dict[str, Any]:
    run_dir = EXPORTS / "tbprofiler"
    run_dir.mkdir(parents=True, exist_ok=True)

    if not fasta_files:
        return {
            "status": "skipped",
            "message": "No FASTA inputs detected for tb-profiler run",
            "attempted_samples": 0,
            "successful_samples": 0,
            "failed_samples": 0,
            "output_jsons": [],
            "failures": [],
        }

    sample_inputs: list[tuple[str, str]] = []
    for fasta in fasta_files[:2]:
        for sample_id, sequence in _split_fasta_records(fasta, max_records=10):
            sample_inputs.append((sample_id, sequence))
            if len(sample_inputs) >= 10:
                break
        if len(sample_inputs) >= 10:
            break

    output_jsons: list[str] = []
    failures: list[dict[str, Any]] = []
    successful_samples = 0

    with tempfile.TemporaryDirectory(prefix="tbprofiler_") as tmp_dir:
        tmp_base = Path(tmp_dir)
        for sample_id, sequence in sample_inputs:
            sample_fasta = tmp_base / f"{sample_id}.fasta"
            sample_fasta.write_text(f">{sample_id}\n{sequence}\n", encoding="utf-8")

            cmd = _build_exec_command(tbprofiler_exec, [
                "profile",
                "--fasta",
                str(sample_fasta),
                "--prefix",
                sample_id,
                "--dir",
                str(run_dir),
            ])

            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=600,
                cwd=str(ROOT),
                check=False,
            )

            result_json = run_dir / "results" / f"{sample_id}.results.json"
            if proc.returncode == 0 and result_json.exists():
                successful_samples += 1
                output_jsons.append(str(result_json.as_posix()))
            else:
                tail = "\n".join((proc.stdout or "").splitlines()[-20:])
                failures.append(
                    {
                        "sample_id": sample_id,
                        "exit_code": proc.returncode,
                        "output_tail": tail,
                    }
                )

    status = "completed" if successful_samples else "failed"
    return {
        "status": status,
        "runner": "local",
        "message": "tb-profiler execution finished" if successful_samples else "tb-profiler execution failed",
        "attempted_samples": len(sample_inputs),
        "successful_samples": successful_samples,
        "failed_samples": len(sample_inputs) - successful_samples,
        "output_jsons": output_jsons,
        "failures": failures[:10],
    }


def _run_tbprofiler_on_fasta_docker(fasta_files: list[Path], image: str) -> dict[str, Any]:
    run_dir = EXPORTS / "tbprofiler"
    run_dir.mkdir(parents=True, exist_ok=True)

    if not fasta_files:
        return {
            "status": "skipped",
            "runner": "docker",
            "message": "No FASTA inputs detected for tb-profiler run",
            "attempted_samples": 0,
            "successful_samples": 0,
            "failed_samples": 0,
            "output_jsons": [],
            "failures": [],
        }

    sample_inputs: list[tuple[str, str]] = []
    for fasta in fasta_files[:2]:
        for sample_id, sequence in _split_fasta_records(fasta, max_records=10):
            sample_inputs.append((sample_id, sequence))
            if len(sample_inputs) >= 10:
                break
        if len(sample_inputs) >= 10:
            break

    output_jsons: list[str] = []
    failures: list[dict[str, Any]] = []
    successful_samples = 0

    with tempfile.TemporaryDirectory(prefix="tbprofiler_input_", dir=str(EXPORTS)) as tmp_dir:
        tmp_base = Path(tmp_dir)
        for sample_id, sequence in sample_inputs:
            sample_fasta = tmp_base / f"{sample_id}.fasta"
            sample_fasta.write_text(f">{sample_id}\n{sequence}\n", encoding="utf-8")

            container_fasta = _to_container_path(sample_fasta)
            container_run_dir = _to_container_path(run_dir)

            cmd = [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{ROOT}:/work",
                "-w",
                "/work",
                image,
                "tb-profiler",
                "profile",
                "--fasta",
                container_fasta,
                "--prefix",
                sample_id,
                "--dir",
                container_run_dir,
            ]

            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=1200,
                cwd=str(ROOT),
                check=False,
            )

            result_json = run_dir / "results" / f"{sample_id}.results.json"
            if proc.returncode == 0 and result_json.exists():
                successful_samples += 1
                output_jsons.append(str(result_json.as_posix()))
            else:
                tail = "\n".join((proc.stdout or "").splitlines()[-20:])
                failures.append(
                    {
                        "sample_id": sample_id,
                        "exit_code": proc.returncode,
                        "output_tail": tail,
                    }
                )

    status = "completed" if successful_samples else "failed"
    return {
        "status": status,
        "runner": "docker",
        "image": image,
        "message": "tb-profiler docker execution finished" if successful_samples else "tb-profiler docker execution failed",
        "attempted_samples": len(sample_inputs),
        "successful_samples": successful_samples,
        "failed_samples": len(sample_inputs) - successful_samples,
        "output_jsons": output_jsons,
        "failures": failures[:10],
    }


def _run_tbprofiler_on_fasta_wsl(fasta_files: list[Path], wsl_env: str) -> dict[str, Any]:
    run_dir = EXPORTS / "tbprofiler"
    run_dir.mkdir(parents=True, exist_ok=True)

    if not fasta_files:
        return {
            "status": "skipped",
            "runner": "wsl",
            "message": "No FASTA inputs detected for tb-profiler run",
            "attempted_samples": 0,
            "successful_samples": 0,
            "failed_samples": 0,
            "output_jsons": [],
            "failures": [],
        }

    sample_inputs: list[tuple[str, str]] = []
    for fasta in fasta_files[:2]:
        for sample_id, sequence in _split_fasta_records(fasta, max_records=10):
            sample_inputs.append((sample_id, sequence))
            if len(sample_inputs) >= 10:
                break
        if len(sample_inputs) >= 10:
            break

    output_jsons: list[str] = []
    failures: list[dict[str, Any]] = []
    successful_samples = 0

    run_dir_wsl = _win_to_wsl_path(run_dir)
    if not run_dir_wsl:
        return {
            "status": "failed",
            "runner": "wsl",
            "message": "Unable to map run directory to WSL path",
            "attempted_samples": 0,
            "successful_samples": 0,
            "failed_samples": 0,
            "output_jsons": [],
            "failures": [{"sample_id": "n/a", "exit_code": 1, "output_tail": "wslpath conversion failed"}],
        }

    with tempfile.TemporaryDirectory(prefix="tbprofiler_wsl_") as tmp_dir:
        tmp_base = Path(tmp_dir)
        for sample_id, sequence in sample_inputs:
            sample_fasta = tmp_base / f"{sample_id}.fasta"
            sample_fasta.write_text(f">{sample_id}\n{sequence}\n", encoding="utf-8")

            sample_fasta_wsl = _win_to_wsl_path(sample_fasta)
            if not sample_fasta_wsl:
                failures.append(
                    {
                        "sample_id": sample_id,
                        "exit_code": 1,
                        "output_tail": "Unable to map sample FASTA to WSL path",
                    }
                )
                continue

            bash_cmd = (
                f'$HOME/micromamba run -n {wsl_env} tb-profiler profile '
                f'--fasta "{sample_fasta_wsl}" '
                f'--prefix "{sample_id}" '
                f'--dir "{run_dir_wsl}"'
            )
            proc = subprocess.run(
                ["wsl", "--", "bash", "-lc", bash_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=1800,
                cwd=str(ROOT),
                check=False,
            )

            result_json = run_dir / "results" / f"{sample_id}.results.json"
            if proc.returncode == 0 and result_json.exists():
                successful_samples += 1
                output_jsons.append(str(result_json.as_posix()))
            else:
                tail = "\n".join((proc.stdout or "").splitlines()[-20:])
                failures.append(
                    {
                        "sample_id": sample_id,
                        "exit_code": proc.returncode,
                        "output_tail": tail,
                    }
                )

    status = "completed" if successful_samples else "failed"
    return {
        "status": status,
        "runner": "wsl",
        "wsl_env": wsl_env,
        "message": "tb-profiler WSL execution finished" if successful_samples else "tb-profiler WSL execution failed",
        "attempted_samples": len(sample_inputs),
        "successful_samples": successful_samples,
        "failed_samples": len(sample_inputs) - successful_samples,
        "output_jsons": output_jsons,
        "failures": failures[:10],
    }


def _run_mykrobe_on_fasta_wsl(fasta_files: list[Path], wsl_env: str) -> dict[str, Any]:
    run_dir = EXPORTS / "mykrobe"
    run_dir.mkdir(parents=True, exist_ok=True)

    if not fasta_files:
        return {
            "status": "skipped",
            "runner": "wsl",
            "message": "No FASTA inputs detected for mykrobe run",
            "attempted_samples": 0,
            "successful_samples": 0,
            "failed_samples": 0,
            "output_jsons": [],
            "failures": [],
        }

    sample_inputs: list[tuple[str, str]] = []
    for fasta in fasta_files[:2]:
        for sample_id, sequence in _split_fasta_records(fasta, max_records=10):
            sample_inputs.append((sample_id, sequence))
            if len(sample_inputs) >= 10:
                break
        if len(sample_inputs) >= 10:
            break

    output_jsons: list[str] = []
    failures: list[dict[str, Any]] = []
    successful_samples = 0

    run_dir_wsl = _win_to_wsl_path(run_dir)
    if not run_dir_wsl:
        return {
            "status": "failed",
            "runner": "wsl",
            "message": "Unable to map run directory to WSL path",
            "attempted_samples": 0,
            "successful_samples": 0,
            "failed_samples": 0,
            "output_jsons": [],
            "failures": [{"sample_id": "n/a", "exit_code": 1, "output_tail": "wslpath conversion failed"}],
        }

    with tempfile.TemporaryDirectory(prefix="mykrobe_wsl_") as tmp_dir:
        tmp_base = Path(tmp_dir)
        for sample_id, sequence in sample_inputs:
            sample_fasta = tmp_base / f"{sample_id}.fasta"
            sample_fasta.write_text(f">{sample_id}\n{sequence}\n", encoding="utf-8")

            sample_fasta_wsl = _win_to_wsl_path(sample_fasta)
            if not sample_fasta_wsl:
                failures.append(
                    {
                        "sample_id": sample_id,
                        "exit_code": 1,
                        "output_tail": "Unable to map sample FASTA to WSL path",
                    }
                )
                continue

            result_json = run_dir / f"{sample_id}_mykrobe.json"
            result_json_wsl = _win_to_wsl_path(result_json)

            bash_cmd = (
                f'$HOME/micromamba run -n {wsl_env} mykrobe predict '
                f'--sample "{sample_id}" '
                f'--seq "{sample_fasta_wsl}" '
                f'--species tb '
                f'--format json '
                f'--output "{result_json_wsl}"'
            )
            proc = subprocess.run(
                ["wsl", "--", "bash", "-lc", bash_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=1800,
                cwd=str(ROOT),
                check=False,
            )

            if proc.returncode == 0 and result_json.exists():
                successful_samples += 1
                output_jsons.append(str(result_json.as_posix()))
            else:
                tail = "\n".join((proc.stdout or "").splitlines()[-20:])
                failures.append(
                    {
                        "sample_id": sample_id,
                        "exit_code": proc.returncode,
                        "output_tail": tail,
                    }
                )

    status = "completed" if successful_samples else "failed"
    return {
        "status": status,
        "runner": "wsl",
        "wsl_env": wsl_env,
        "message": "mykrobe WSL execution finished" if successful_samples else "mykrobe WSL execution failed",
        "attempted_samples": len(sample_inputs),
        "successful_samples": successful_samples,
        "failed_samples": len(sample_inputs) - successful_samples,
        "output_jsons": output_jsons,
        "failures": failures[:10],
    }


def _extract_mykrobe_results(result_json: Path) -> dict[str, Any]:
    payload = json.loads(result_json.read_text(encoding="utf-8"))

    # mykrobe JSON is keyed by sample_id at the top level
    sample_key = next(iter(payload), None) if isinstance(payload, dict) else None
    sample_data = payload.get(sample_key, {}) if sample_key else {}

    # Lineage: payload[sample]["phylogenetics"]["lineage"] → dict of {lineage_name: {percent_coverage, ...}}
    phylo = sample_data.get("phylogenetics", {})
    lineage_dict = phylo.get("lineage", {})
    if lineage_dict:
        # Pick the lineage with highest percent_coverage
        best = max(lineage_dict.items(), key=lambda kv: kv[1].get("percent_coverage", 0) if isinstance(kv[1], dict) else 0)
        lineage_value: str | None = str(best[0])
    else:
        lineage_value = None

    # DR susceptibility: payload[sample]["susceptibility"] → {Drug: {predict: "S"/"R"/"N"}}
    susceptibility = sample_data.get("susceptibility", {})
    dr_payload: dict[str, str] | None = None
    if susceptibility:
        dr_payload = {drug: info.get("predict", "U") for drug, info in susceptibility.items() if isinstance(info, dict)}

    return {
        "lineage": lineage_value,
        "predicted_drug_resistance": dr_payload,
        "confidence_score": None,
        "interpretation_summary": (
            ", ".join(f"{d}:{p}" for d, p in dr_payload.items() if p in {"R", "r"})
            if dr_payload
            else None
        ),
    }


def _import_mykrobe_results(result_json_paths: list[str]) -> dict[str, Any]:
    db = SessionLocal()
    imported = 0
    skipped = 0
    warnings: list[str] = []

    try:
        for path_str in result_json_paths:
            path = Path(path_str)
            # Filename: <sample_id>_mykrobe.json
            sample_id = path.stem.replace("_mykrobe", "")

            exists = db.execute(
                text("SELECT 1 FROM cases WHERE pseudonymised_case_id = CAST(:sample_id AS uuid)"),
                {"sample_id": sample_id},
            ).scalar()
            if not exists:
                skipped += 1
                warnings.append(f"sample_id not found in cases ({sample_id})")
                continue

            fields = _extract_mykrobe_results(path)
            db.execute(
                text(
                    """
                    INSERT INTO tb_interpretation (
                        sample_id,
                        species_confirmation,
                        lineage,
                        predicted_drug_resistance,
                        confidence_score,
                        interpretation_summary
                    )
                    VALUES (
                        CAST(:sample_id AS uuid),
                        :species_confirmation,
                        :lineage,
                        CAST(:predicted_drug_resistance AS jsonb),
                        :confidence_score,
                        :interpretation_summary
                    )
                    ON CONFLICT (sample_id) DO UPDATE SET
                        lineage = COALESCE(EXCLUDED.lineage, tb_interpretation.lineage),
                        predicted_drug_resistance = COALESCE(EXCLUDED.predicted_drug_resistance, tb_interpretation.predicted_drug_resistance),
                        confidence_score = COALESCE(EXCLUDED.confidence_score, tb_interpretation.confidence_score),
                        interpretation_summary = COALESCE(EXCLUDED.interpretation_summary, tb_interpretation.interpretation_summary)
                    """
                ),
                {
                    "sample_id": sample_id,
                    "species_confirmation": "M. tuberculosis complex",
                    "lineage": fields["lineage"],
                    "predicted_drug_resistance": json.dumps(fields["predicted_drug_resistance"]) if fields["predicted_drug_resistance"] is not None else "null",
                    "confidence_score": fields["confidence_score"],
                    "interpretation_summary": fields["interpretation_summary"],
                },
            )
            imported += 1

        db.commit()
        return {
            "status": "imported",
            "message": "mykrobe results imported into tb_interpretation",
            "imported_rows": imported,
            "skipped_rows": skipped,
            "warnings": warnings[:25],
        }
    except Exception as exc:
        db.rollback()
        return {
            "status": "failed",
            "message": str(exc),
            "imported_rows": imported,
            "skipped_rows": skipped,
            "warnings": warnings[:25],
        }
    finally:
        db.close()


def _import_tbprofiler_results(result_json_paths: list[str]) -> dict[str, Any]:
    db = SessionLocal()
    imported = 0
    skipped = 0
    warnings: list[str] = []

    try:
        for path_str in result_json_paths:
            path = Path(path_str)
            sample_id = path.stem.replace(".results", "")

            exists = db.execute(
                text("SELECT 1 FROM cases WHERE pseudonymised_case_id = CAST(:sample_id AS uuid)"),
                {"sample_id": sample_id},
            ).scalar()
            if not exists:
                skipped += 1
                warnings.append(f"sample_id not found in cases ({sample_id})")
                continue

            fields = _extract_lineage_and_resistance(path)
            db.execute(
                text(
                    """
                    INSERT INTO tb_interpretation (
                        sample_id,
                        species_confirmation,
                        lineage,
                        predicted_drug_resistance,
                        confidence_score,
                        interpretation_summary
                    )
                    VALUES (
                        CAST(:sample_id AS uuid),
                        :species_confirmation,
                        :lineage,
                        CAST(:predicted_drug_resistance AS jsonb),
                        :confidence_score,
                        :interpretation_summary
                    )
                    ON CONFLICT (sample_id) DO UPDATE SET
                        lineage = EXCLUDED.lineage,
                        predicted_drug_resistance = EXCLUDED.predicted_drug_resistance,
                        confidence_score = EXCLUDED.confidence_score,
                        interpretation_summary = EXCLUDED.interpretation_summary
                    """
                ),
                {
                    "sample_id": sample_id,
                    "species_confirmation": "M. tuberculosis complex",
                    "lineage": fields["lineage"],
                    "predicted_drug_resistance": json.dumps(fields["predicted_drug_resistance"]) if fields["predicted_drug_resistance"] is not None else "null",
                    "confidence_score": fields["confidence_score"],
                    "interpretation_summary": fields["interpretation_summary"],
                },
            )
            imported += 1

        db.commit()
        return {
            "status": "imported",
            "message": "tb-profiler results imported into tb_interpretation",
            "imported_rows": imported,
            "skipped_rows": skipped,
            "warnings": warnings[:25],
        }
    except Exception as exc:
        db.rollback()
        return {
            "status": "failed",
            "message": str(exc),
            "imported_rows": imported,
            "skipped_rows": skipped,
            "warnings": warnings[:25],
        }
    finally:
        db.close()


def _parse_resistance(value: str) -> Any:
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return {"summary": stripped}


def _to_float(value: str) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        return None


def _import_calls_csv(path: Path) -> dict[str, Any]:
    db = SessionLocal()
    imported = 0
    skipped = 0
    warnings: list[str] = []

    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            required = {"sample_id", "lineage", "predicted_drug_resistance", "confidence_score", "interpretation_summary"}
            header = set(reader.fieldnames or [])
            missing = sorted(required - header)
            if missing:
                return {
                    "status": "invalid_csv",
                    "message": f"Missing required columns: {missing}",
                    "imported_rows": 0,
                    "skipped_rows": 0,
                    "warnings": [],
                }

            for index, row in enumerate(reader, start=2):
                sample_id = (row.get("sample_id") or "").strip()
                if not sample_id:
                    skipped += 1
                    warnings.append(f"row {index}: empty sample_id")
                    continue

                exists = db.execute(
                    text("SELECT 1 FROM cases WHERE pseudonymised_case_id = CAST(:sample_id AS uuid)"),
                    {"sample_id": sample_id},
                ).scalar()
                if not exists:
                    skipped += 1
                    warnings.append(f"row {index}: sample_id not found in cases ({sample_id})")
                    continue

                lineage = (row.get("lineage") or "").strip() or None
                resistance_obj = _parse_resistance(row.get("predicted_drug_resistance") or "")
                confidence_score = _to_float(row.get("confidence_score") or "")
                interpretation_summary = (row.get("interpretation_summary") or "").strip() or None

                db.execute(
                    text(
                        """
                        INSERT INTO tb_interpretation (
                            sample_id,
                            species_confirmation,
                            lineage,
                            predicted_drug_resistance,
                            confidence_score,
                            interpretation_summary
                        )
                        VALUES (
                            CAST(:sample_id AS uuid),
                            :species_confirmation,
                            :lineage,
                            CAST(:predicted_drug_resistance AS jsonb),
                            :confidence_score,
                            :interpretation_summary
                        )
                        ON CONFLICT (sample_id) DO UPDATE SET
                            lineage = COALESCE(EXCLUDED.lineage, tb_interpretation.lineage),
                            predicted_drug_resistance = COALESCE(EXCLUDED.predicted_drug_resistance, tb_interpretation.predicted_drug_resistance),
                            confidence_score = COALESCE(EXCLUDED.confidence_score, tb_interpretation.confidence_score),
                            interpretation_summary = COALESCE(EXCLUDED.interpretation_summary, tb_interpretation.interpretation_summary)
                        """
                    ),
                    {
                        "sample_id": sample_id,
                        "species_confirmation": "M. tuberculosis complex",
                        "lineage": lineage,
                        "predicted_drug_resistance": json.dumps(resistance_obj) if resistance_obj is not None else "null",
                        "confidence_score": confidence_score,
                        "interpretation_summary": interpretation_summary,
                    },
                )
                imported += 1

        db.commit()
        return {
            "status": "imported",
            "message": "Lineage/DR calls imported into tb_interpretation",
            "imported_rows": imported,
            "skipped_rows": skipped,
            "warnings": warnings[:25],
        }
    except Exception as exc:
        db.rollback()
        return {
            "status": "failed",
            "message": str(exc),
            "imported_rows": imported,
            "skipped_rows": skipped,
            "warnings": warnings[:25],
        }
    finally:
        db.close()


def _normalise_dr_to_rs(dr: Any) -> dict[str, str]:
    """Flatten any DR payload shape to {drug: 'R' | 'S'}. Only R and S are kept."""
    result: dict[str, str] = {}
    if isinstance(dr, dict):
        for drug, val in dr.items():
            if isinstance(val, str):
                result[drug] = val.upper()[:1]
            elif isinstance(val, dict):
                predict = val.get("predict") or val.get("call") or val.get("type") or ""
                if predict:
                    result[drug] = str(predict).upper()[:1]
                elif val.get("variants"):
                    result[drug] = "R"
            elif isinstance(val, list) and val:
                result[drug] = "R"
    elif isinstance(dr, list):
        for item in dr:
            if isinstance(item, dict):
                drug = item.get("drug") or item.get("Drug") or ""
                call = item.get("call") or item.get("prediction") or item.get("type") or "R"
                if drug:
                    result[drug] = str(call).upper()[:1]
    return {k: v for k, v in result.items() if v in {"R", "S"}}


def _detect_dr_discordance(
    tbprofiler_jsons: list[str],
    mykrobe_jsons: list[str],
) -> list[dict[str, Any]]:
    """Compare per-sample DR calls from both tools; return discordances (one says R, other says S)."""
    tbp_by_sample: dict[str, dict[str, str]] = {}
    for path_str in tbprofiler_jsons:
        path = Path(path_str)
        sample_id = path.stem.replace(".results", "")
        fields = _extract_lineage_and_resistance(path)
        dr = fields.get("predicted_drug_resistance")
        if dr is not None:
            tbp_by_sample[sample_id] = _normalise_dr_to_rs(dr)

    mk_by_sample: dict[str, dict[str, str]] = {}
    for path_str in mykrobe_jsons:
        path = Path(path_str)
        sample_id = path.stem.replace("_mykrobe", "")
        fields = _extract_mykrobe_results(path)
        dr = fields.get("predicted_drug_resistance")
        if dr is not None:
            mk_by_sample[sample_id] = _normalise_dr_to_rs(dr)

    discordances: list[dict[str, Any]] = []
    for sample_id in set(tbp_by_sample) & set(mk_by_sample):
        tbp_dr = tbp_by_sample[sample_id]
        mk_dr = mk_by_sample[sample_id]
        discordant_drugs: list[dict[str, str]] = []
        for drug in set(tbp_dr) & set(mk_dr):
            if {tbp_dr[drug], mk_dr[drug]} == {"R", "S"}:
                discordant_drugs.append(
                    {"drug": drug, "tbprofiler": tbp_dr[drug], "mykrobe": mk_dr[drug]}
                )
        discordances.append({
            "sample_id": sample_id,
            "concordant": len(discordant_drugs) == 0,
            "discordant_drugs": discordant_drugs,
            "drugs_compared": len(set(tbp_dr) & set(mk_dr)),
        })

    return discordances


def _log_concordance_to_audit(discordances: list[dict[str, Any]]) -> None:
    """Write DR concordance results to audit_log; one row per discordant sample."""
    discordant_samples = [d for d in discordances if not d["concordant"]]
    if not discordant_samples:
        return
    db = SessionLocal()
    try:
        for item in discordant_samples:
            db.execute(
                text(
                    "INSERT INTO audit_log (action, details, timestamp) "
                    "VALUES (:action, CAST(:details AS jsonb), NOW())"
                ),
                {
                    "action": "dr_concordance_discordance_flagged",
                    "details": json.dumps(item),
                },
            )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def main() -> None:
    EXPORTS.mkdir(parents=True, exist_ok=True)

    docker = _probe_docker()
    wsl = _probe_wsl()
    docker_image = os.getenv("TBPROFILER_DOCKER_IMAGE", "quay.io/jodyphelan/tbprofiler:latest")
    docker_fallback_enabled = os.getenv("TBPROFILER_DOCKER_FALLBACK", "1") == "1"
    wsl_fallback_enabled = os.getenv("TBPROFILER_WSL_FALLBACK", "1") == "1"
    wsl_env_name = os.getenv("TBPROFILER_WSL_ENV", "tbtools")

    tbprofiler_exec = _which_many(["tb-profiler", "tb-profiler.exe", "tb_profiler", "tb-profiler-tools"])
    mykrobe_exec = _which_many(["mykrobe", "mykrobe.exe"])
    inputs = _discover_inputs()
    fasta_inputs = _discover_fasta_inputs()

    tbprofiler = {
        "status": "not_installed",
        "executable": None,
        "version_probe": None,
        "message": "tb-profiler not found in PATH",
    }
    if tbprofiler_exec:
        probe_status, probe_output = _probe_version(tbprofiler_exec, ["--version"])
        tbprofiler = {
            "status": "installed" if probe_status == "ok" else "installed_but_unusable",
            "executable": tbprofiler_exec,
            "version_probe": {"status": probe_status, "output": probe_output},
            "message": "tb-profiler detected" if probe_status == "ok" else "tb-profiler executable found but version probe failed",
        }

    mykrobe = {
        "status": "not_installed",
        "executable": None,
        "version_probe": None,
        "message": "mykrobe not found in PATH",
    }
    if mykrobe_exec:
        probe_status, probe_output = _probe_version(mykrobe_exec, ["--version"])
        mykrobe = {
            "status": "installed",
            "executable": mykrobe_exec,
            "version_probe": {"status": probe_status, "output": probe_output},
            "message": "mykrobe detected",
        }

    wsl_tbprofiler = {
        "status": "not_checked",
        "probe": None,
        "message": "WSL tool probe not run",
    }
    wsl_mykrobe = {
        "status": "not_checked",
        "probe": None,
        "message": "WSL tool probe not run",
    }
    if wsl.get("available") and wsl_fallback_enabled:
        probe_status, probe_output = _wsl_probe_tool(f"$HOME/micromamba run -n {wsl_env_name} tb-profiler version")
        wsl_tbprofiler = {
            "status": "installed" if probe_status == "ok" else "not_ready",
            "probe": {"status": probe_status, "output": probe_output},
            "message": "tb-profiler available in WSL env" if probe_status == "ok" else "tb-profiler unavailable in WSL env",
        }

        mk_status, mk_output = _wsl_probe_tool(f"$HOME/micromamba run -n {wsl_env_name} mykrobe --help")
        wsl_mykrobe = {
            "status": "installed" if mk_status == "ok" else "not_ready",
            "probe": {"status": mk_status, "output": mk_output},
            "message": "mykrobe available in WSL env" if mk_status == "ok" else "mykrobe unavailable in WSL env",
        }

    import_candidates = [
        EXPORTS / "lineage_resistance_calls.csv",
        UPLOADS / "lineage_resistance_calls.csv",
    ]
    import_path = next((p for p in import_candidates if p.exists()), None)
    import_result: dict[str, Any]
    if import_path:
        import_result = _import_calls_csv(import_path)
        import_result["source_csv"] = str(import_path.as_posix())
    else:
        import_result = {
            "status": "skipped",
            "message": "No lineage_resistance_calls.csv found in exports/ or uploads/",
            "imported_rows": 0,
            "skipped_rows": 0,
            "warnings": [],
        }

    tbprofiler_run = {
        "status": "skipped",
        "message": "tb-profiler not ready to run",
        "runner": "none",
        "attempted_samples": 0,
        "successful_samples": 0,
        "failed_samples": 0,
        "output_jsons": [],
        "failures": [],
    }
    tbprofiler_local_run = {
        "status": "skipped",
        "runner": "local",
        "message": "Local tb-profiler execution not attempted",
        "attempted_samples": 0,
        "successful_samples": 0,
        "failed_samples": 0,
        "output_jsons": [],
        "failures": [],
    }
    tbprofiler_docker_run = {
        "status": "skipped",
        "runner": "docker",
        "message": "Docker tb-profiler execution not attempted",
        "attempted_samples": 0,
        "successful_samples": 0,
        "failed_samples": 0,
        "output_jsons": [],
        "failures": [],
    }
    tbprofiler_wsl_run = {
        "status": "skipped",
        "runner": "wsl",
        "message": "WSL tb-profiler execution not attempted",
        "attempted_samples": 0,
        "successful_samples": 0,
        "failed_samples": 0,
        "output_jsons": [],
        "failures": [],
    }
    tbprofiler_import = {
        "status": "skipped",
        "message": "No tb-profiler result JSON files imported",
        "imported_rows": 0,
        "skipped_rows": 0,
        "warnings": [],
    }

    mykrobe_run = {
        "status": "skipped",
        "message": "mykrobe not ready to run",
        "runner": "none",
        "attempted_samples": 0,
        "successful_samples": 0,
        "failed_samples": 0,
        "output_jsons": [],
        "failures": [],
    }
    mykrobe_wsl_run = {
        "status": "skipped",
        "runner": "wsl",
        "message": "WSL mykrobe execution not attempted",
        "attempted_samples": 0,
        "successful_samples": 0,
        "failed_samples": 0,
        "output_jsons": [],
        "failures": [],
    }
    mykrobe_import = {
        "status": "skipped",
        "message": "No mykrobe result JSON files imported",
        "imported_rows": 0,
        "skipped_rows": 0,
        "warnings": [],
    }

    if tbprofiler["status"] == "installed" and fasta_inputs:
        tbprofiler_local_run = _run_tbprofiler_on_fasta(tbprofiler_exec, fasta_inputs)
        tbprofiler_run = tbprofiler_local_run
    elif tbprofiler["status"] == "installed_but_unusable":
        tbprofiler_local_run = {
            "status": "failed",
            "runner": "local",
            "message": "Local tb-profiler executable is present but failed version probe",
            "attempted_samples": 0,
            "successful_samples": 0,
            "failed_samples": 0,
            "output_jsons": [],
            "failures": [],
        }

    if (
        wsl_fallback_enabled
        and fasta_inputs
        and wsl.get("available")
        and wsl_tbprofiler.get("status") == "installed"
        and (
            tbprofiler_run["status"] in {"skipped", "failed"}
            or tbprofiler["status"] in {"not_installed", "installed_but_unusable"}
        )
    ):
        tbprofiler_wsl_run = _run_tbprofiler_on_fasta_wsl(fasta_inputs, wsl_env_name)
        if tbprofiler_wsl_run["status"] == "completed":
            tbprofiler_run = tbprofiler_wsl_run
        elif tbprofiler_run["status"] == "skipped":
            tbprofiler_run = tbprofiler_wsl_run

    if (
        docker_fallback_enabled
        and fasta_inputs
        and docker.get("available")
        and docker.get("daemon_running")
        and (
            tbprofiler_run["status"] in {"skipped", "failed"}
            or tbprofiler["status"] in {"not_installed", "installed_but_unusable"}
        )
    ):
        tbprofiler_docker_run = _run_tbprofiler_on_fasta_docker(fasta_inputs, docker_image)
        if tbprofiler_docker_run["status"] == "completed":
            tbprofiler_run = tbprofiler_docker_run
        elif tbprofiler_run["status"] == "skipped":
            tbprofiler_run = tbprofiler_docker_run

    if tbprofiler_run["status"] == "completed" and tbprofiler_run["output_jsons"]:
        pass  # imported below after mykrobe, so tbprofiler overwrites as authoritative

    # Run mykrobe via WSL in parallel with tb-profiler — always when available and FASTA inputs exist.
    if (
        wsl_fallback_enabled
        and fasta_inputs
        and wsl.get("available")
        and wsl_mykrobe.get("status") == "installed"
    ):
        mykrobe_wsl_run = _run_mykrobe_on_fasta_wsl(fasta_inputs, wsl_env_name)
        mykrobe_run = mykrobe_wsl_run

    # Import order: Mykrobe first (fills base data), TBProfiler second (overwrites as authoritative).
    if mykrobe_run["status"] == "completed" and mykrobe_run["output_jsons"]:
        mykrobe_import = _import_mykrobe_results(mykrobe_run["output_jsons"])

    if tbprofiler_run["status"] == "completed" and tbprofiler_run["output_jsons"]:
        tbprofiler_import = _import_tbprofiler_results(tbprofiler_run["output_jsons"])

    # Discordance check: flag samples where both tools ran but disagree on R/S.
    dr_concordance: list[dict[str, Any]] = []
    if tbprofiler_run["output_jsons"] and mykrobe_run["output_jsons"]:
        dr_concordance = _detect_dr_discordance(
            tbprofiler_run["output_jsons"], mykrobe_run["output_jsons"]
        )
        _log_concordance_to_audit(dr_concordance)

    ready_inputs = inputs["fastq_count"] > 0 or inputs["vcf_count"] > 0 or inputs["fasta_count"] > 0
    overall_status = "completed"
    if tbprofiler["status"] not in {"installed", "installed_but_unusable"} and mykrobe["status"] != "installed":
        overall_status = "blocked_no_tools"
    elif tbprofiler["status"] == "installed_but_unusable" and mykrobe["status"] != "installed":
        overall_status = "blocked_tool_dependencies"
    elif not ready_inputs:
        overall_status = "ready_missing_inputs"
    elif tbprofiler_run["status"] == "failed":
        overall_status = "failed_tbprofiler_runtime"

    if tbprofiler_run["status"] == "completed":
        overall_status = "completed"

    if mykrobe_run["status"] == "completed" and overall_status != "completed":
        overall_status = "completed"

    payload = {
        "generated_at": _iso_now(),
        "status": overall_status,
        "scaffold_version": "1.0",
        "engines": {
            "tb_profiler": tbprofiler,
            "mykrobe": mykrobe,
        },
        "docker": {
            "available": docker["available"],
            "status": docker["status"],
            "probe": docker["output"],
            "daemon_running": docker.get("daemon_running", False),
            "daemon_probe": docker.get("daemon_probe", "unknown"),
            "fallback_enabled": docker_fallback_enabled,
            "tbprofiler_image": docker_image,
        },
        "wsl": {
            "available": wsl["available"],
            "status": wsl["status"],
            "probe": wsl["output"],
            "details": wsl.get("details", []),
            "fallback_enabled": wsl_fallback_enabled,
            "env": wsl_env_name,
            "tbprofiler": wsl_tbprofiler,
            "mykrobe": wsl_mykrobe,
        },
        "inputs": inputs,
        "tbprofiler_run": tbprofiler_run,
        "tbprofiler_local_run": tbprofiler_local_run,
        "tbprofiler_wsl_run": tbprofiler_wsl_run,
        "tbprofiler_docker_run": tbprofiler_docker_run,
        "db_import": import_result,
        "tbprofiler_db_import": tbprofiler_import,
        "mykrobe_run": mykrobe_run,
        "mykrobe_wsl_run": mykrobe_wsl_run,
        "mykrobe_db_import": mykrobe_import,
        "dr_concordance": {
            "samples_compared": len(dr_concordance),
            "all_concordant": all(d["concordant"] for d in dr_concordance) if dr_concordance else None,
            "discordant_sample_count": sum(1 for d in dr_concordance if not d["concordant"]),
            "details": dr_concordance,
        },
        "next_steps": [
            "Install tb-profiler/mykrobe dependencies (WSL2 micromamba env is supported via TBPROFILER_WSL_FALLBACK=1).",
            "If local dependencies fail, install Docker Desktop and rerun with TBPROFILER_DOCKER_FALLBACK=1.",
            "Place FASTQ/VCF/FASTA inputs in uploads/ (or export pipeline outputs there).",
            "Optionally provide exports/lineage_resistance_calls.csv to hydrate tb_interpretation.",
        ],
    }

    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
