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
    return None


def _probe_version(exec_path: str, args: list[str]) -> tuple[str, str]:
    try:
        proc = subprocess.run(
            [exec_path, *args],
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
    first_line = output.splitlines()[0][:200]
    return "ok", first_line


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

    tbprofiler_exec = _which_many(["tb-profiler", "tb-profiler.exe", "tb_profiler"])
    mykrobe_exec = _which_many(["mykrobe", "mykrobe.exe"])
    inputs = _discover_inputs()

    tbprofiler = {
        "status": "not_installed",
        "executable": None,
        "version_probe": None,
        "message": "tb-profiler not found in PATH",
    }
    if tbprofiler_exec:
        probe_status, probe_output = _probe_version(tbprofiler_exec, ["--version"])
        tbprofiler = {
            "status": "installed",
            "executable": tbprofiler_exec,
            "version_probe": {"status": probe_status, "output": probe_output},
            "message": "tb-profiler detected",
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

    ready_inputs = inputs["fastq_count"] > 0 or inputs["vcf_count"] > 0 or inputs["fasta_count"] > 0
    overall_status = "completed"
    if tbprofiler["status"] != "installed" and mykrobe["status"] != "installed":
        overall_status = "blocked_no_tools"
    elif not ready_inputs:
        overall_status = "ready_missing_inputs"

    payload = {
        "generated_at": _iso_now(),
        "status": overall_status,
        "scaffold_version": "1.0",
        "engines": {
            "tb_profiler": tbprofiler,
            "mykrobe": mykrobe,
        },
        "inputs": inputs,
        "db_import": import_result,
        "next_steps": [
            "Install tb-profiler and/or mykrobe in the analysis environment.",
            "Place FASTQ/VCF/FASTA inputs in uploads/ (or export pipeline outputs there).",
            "Optionally provide exports/lineage_resistance_calls.csv to hydrate tb_interpretation.",
        ],
    }

    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
