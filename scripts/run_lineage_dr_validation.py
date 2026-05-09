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

    lineage = None
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

    return {
        "lineage": lineage,
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


def main() -> None:
    EXPORTS.mkdir(parents=True, exist_ok=True)

    docker = _probe_docker()
    docker_image = os.getenv("TBPROFILER_DOCKER_IMAGE", "quay.io/jodyphelan/tbprofiler:latest")
    docker_fallback_enabled = os.getenv("TBPROFILER_DOCKER_FALLBACK", "1") == "1"

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
    tbprofiler_import = {
        "status": "skipped",
        "message": "No tb-profiler result JSON files imported",
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
        tbprofiler_import = _import_tbprofiler_results(tbprofiler_run["output_jsons"])

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
        "inputs": inputs,
        "tbprofiler_run": tbprofiler_run,
        "tbprofiler_local_run": tbprofiler_local_run,
        "tbprofiler_docker_run": tbprofiler_docker_run,
        "db_import": import_result,
        "tbprofiler_db_import": tbprofiler_import,
        "next_steps": [
            "Install tb-profiler/mykrobe dependencies (for Windows this is often easiest in WSL2 or Conda).",
            "If local dependencies fail, install Docker Desktop and rerun with TBPROFILER_DOCKER_FALLBACK=1.",
            "Place FASTQ/VCF/FASTA inputs in uploads/ (or export pipeline outputs there).",
            "Optionally provide exports/lineage_resistance_calls.csv to hydrate tb_interpretation.",
        ],
    }

    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
