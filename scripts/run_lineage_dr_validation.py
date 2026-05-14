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
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
EXPORTS = ROOT / "exports"
UPLOADS = ROOT / "uploads"
OUT_JSON = EXPORTS / "lineage_dr_validation.json"
RESISTANCE_VALIDATION_JSON = EXPORTS / "resistance_validation.json"
MAX_FASTA_SAMPLES = 10

# Add project root to import path for direct script execution.
sys.path.insert(0, str(ROOT))

from backend.database import SessionLocal


def _json_default(obj: object) -> object:
    """JSON serialization fallback — converts Decimal to float."""
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


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
    explicit = os.getenv("LINEAGE_DR_FASTA") or os.getenv("TB_LINEAGE_DR_FASTA")
    if explicit:
        explicit_path = Path(explicit)
        return [explicit_path] if explicit_path.exists() else []

    exports_fasta = sorted(EXPORTS.glob("*.fasta"), key=lambda p: p.stat().st_mtime, reverse=True)
    upload_fasta = sorted([*UPLOADS.glob("*.fasta"), *UPLOADS.glob("*.fa")], key=lambda p: p.stat().st_mtime, reverse=True)

    dna_export = EXPORTS / "dna.fasta"
    if dna_export.exists():
        return [dna_export]
    if exports_fasta:
        return [exports_fasta[0]]
    if upload_fasta:
        return [upload_fasta[0]]
    return []


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


def _written_during_run(path: Path, started_at: float) -> bool:
    try:
        return path.exists() and path.stat().st_mtime >= started_at
    except OSError:
        return False


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


def _prepare_fasta_sample_inputs(fasta_files: list[Path], max_samples: int = MAX_FASTA_SAMPLES) -> list[dict[str, Any]]:
    sample_inputs: list[dict[str, Any]] = []
    for fasta in fasta_files[:1]:
        for sample_id, sequence in _split_fasta_records(fasta, max_records=max_samples):
            sample_inputs.append(
                {
                    "sample_id": sample_id,
                    "sequence": sequence,
                    "source_fasta": str(fasta.as_posix()),
                }
            )
            if len(sample_inputs) >= max_samples:
                break
    return sample_inputs


def _fasta_input_metadata(fasta_files: list[Path], sample_inputs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "selected_fasta_files": [str(p.as_posix()) for p in fasta_files],
        "selection_policy": (
            "explicit LINEAGE_DR_FASTA/TB_LINEAGE_DR_FASTA path"
            if (os.getenv("LINEAGE_DR_FASTA") or os.getenv("TB_LINEAGE_DR_FASTA"))
            else "exports/dna.fasta, otherwise newest exports FASTA, otherwise newest uploads FASTA"
        ),
        "attempted_sample_ids": [str(item["sample_id"]) for item in sample_inputs],
    }


def _validate_fasta_sample_inputs(sample_inputs: list[dict[str, Any]]) -> dict[str, Any]:
    if not sample_inputs:
        return {
            "status": "no_samples",
            "message": "No FASTA records selected for lineage/DR tool execution",
            "matched_sample_ids": [],
            "unmatched_sample_ids": [],
        }

    db = SessionLocal()
    sample_map = _load_sample_id_map()
    matched: list[str] = []
    unmatched: list[str] = []
    try:
        for item in sample_inputs:
            sample_id = str(item["sample_id"])
            if _resolve_case_sample_id(db, sample_id, sample_map):
                matched.append(sample_id)
            else:
                unmatched.append(sample_id)
    except Exception as exc:
        return {
            "status": "validation_error",
            "message": f"Unable to validate FASTA sample IDs against cases: {exc}",
            "matched_sample_ids": matched,
            "unmatched_sample_ids": unmatched,
        }
    finally:
        db.close()

    if matched and not unmatched:
        status = "matched"
        message = "All selected FASTA sample IDs resolve to active cases"
    elif matched:
        status = "partial_match"
        message = "Some selected FASTA sample IDs do not resolve to active cases"
    else:
        status = "no_matching_cases"
        message = "Selected FASTA sample IDs do not resolve to active cases"

    return {
        "status": status,
        "message": message,
        "matched_sample_ids": matched,
        "unmatched_sample_ids": unmatched,
    }


def _sample_id_map_paths() -> list[Path]:
    return [
        EXPORTS / "sample_id_map.csv",
        EXPORTS / "analysis_sample_map.csv",
        UPLOADS / "sample_id_map.csv",
        UPLOADS / "analysis_sample_map.csv",
    ]


def _load_sample_id_map() -> dict[str, str]:
    """Return optional tool/sample aliases mapped onto case UUIDs."""
    mapping: dict[str, str] = {}
    source_cols = ("tool_sample_id", "sample_id", "analysis_sample_id", "input_sample_id")
    target_cols = ("case_id", "pseudonymised_case_id", "canonical_sample_id")

    for path in _sample_id_map_paths():
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    source = next((row.get(col, "").strip() for col in source_cols if row.get(col, "").strip()), "")
                    target = next((row.get(col, "").strip() for col in target_cols if row.get(col, "").strip()), "")
                    if source and target:
                        mapping[source] = target
        except Exception:
            continue

    return mapping


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except Exception:
        return False


def _resolve_case_sample_id(db: Any, sample_id: str, sample_map: dict[str, str]) -> str | None:
    """Resolve a tool output sample ID to cases.pseudonymised_case_id."""
    candidates = [sample_id]
    if sample_id in sample_map:
        candidates.insert(0, sample_map[sample_id])

    for candidate in candidates:
        if _looks_like_uuid(candidate):
            exists = db.execute(
                text("SELECT 1 FROM cases WHERE pseudonymised_case_id = CAST(:sample_id AS uuid)"),
                {"sample_id": candidate},
            ).scalar()
            if exists:
                return candidate

    # Some pipelines name FASTA/tool outputs using local lab IDs. Support that
    # without changing the canonical tb_interpretation foreign key.
    exists = db.execute(
        text("SELECT pseudonymised_case_id::text FROM cases WHERE local_lab_sample_id = :sample_id"),
        {"sample_id": sample_id},
    ).scalar()
    if exists:
        return str(exists)

    return None


def _import_status(tool_name: str, imported: int, skipped: int) -> tuple[str, str]:
    if imported:
        if skipped:
            return "imported_with_warnings", f"{tool_name} results imported with skipped unlinked samples"
        return "imported", f"{tool_name} results imported into tb_interpretation"
    if skipped:
        return "no_matching_cases", f"{tool_name} results were generated but none matched cases"
    return "no_results", f"No {tool_name} results available to import"


def _normalise_token(value: object) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _expected_genes_for_drug(drug: object) -> list[str]:
    """Local safety-screen map for obvious TB drug/gene parser errors."""
    drug_text = str(drug or "").lower()
    mapping = [
        ("rifabutin", ["rpoB"]),
        ("rifapentine", ["rpoB"]),
        ("rifamp", ["rpoB"]),
        ("isoniazid", ["katG", "inhA", "fabG1", "ahpC", "kasA"]),
        ("prothionamide", ["ethA", "ethR", "inhA", "fabG1", "mshA"]),
        ("ethionamide", ["ethA", "ethR", "inhA", "fabG1", "mshA"]),
        ("pyrazinamide", ["pncA", "rpsA", "panD"]),
        ("ethambutol", ["embB", "embA", "embC", "embR", "iniB"]),
        ("moxifloxacin", ["gyrA", "gyrB"]),
        ("levofloxacin", ["gyrA", "gyrB"]),
        ("ciprofloxacin", ["gyrA", "gyrB"]),
        ("ofloxacin", ["gyrA", "gyrB"]),
        ("gatifloxacin", ["gyrA", "gyrB"]),
        ("fluoroquinolone", ["gyrA", "gyrB"]),
        ("fluoroquin", ["gyrA", "gyrB"]),
        ("amikacin", ["rrs", "eis"]),
        ("kanamycin", ["rrs", "eis"]),
        ("capreomycin", ["rrs", "tlyA"]),
        ("streptomycin", ["rpsL", "rrs", "gid"]),
        ("bedaquiline", ["atpE", "Rv0678", "pepQ", "mmpL5", "mmpS5"]),
        ("linezolid", ["rrl", "rplC"]),
        ("clofazimine", ["Rv0678", "pepQ", "mmpL5", "mmpS5"]),
        ("delamanid", ["ddn", "fgd1", "fbiA", "fbiB", "fbiC"]),
        ("pretomanid", ["ddn", "fgd1", "fbiA", "fbiB", "fbiC", "Rv3547"]),
        ("aminosalicylic", ["thyA", "folC", "thyX"]),
        ("para-amino", ["thyA", "folC", "thyX"]),
        ("terizidone", ["ald", "alr"]),
        ("cycloserine", ["ald", "alr"]),
        ("imipenem", ["blaC"]),
        ("meropenem", ["blaC"]),
        ("clavulanate", ["blaC"]),
    ]
    for marker, genes in mapping:
        if marker in drug_text:
            return genes
    return []


def _drug_gene_mapping_status(drug: object, gene: object) -> str:
    expected = _expected_genes_for_drug(drug)
    gene_text = _normalise_token(gene)
    if not expected or not gene_text or gene_text == "na":
        return "unknown_catalogue_mapping"
    if any(_normalise_token(expected_gene) in gene_text for expected_gene in expected):
        return "expected_gene"
    return "unusual_gene_drug_mapping"


def _coerce_json_value(value: object) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {str(k): _coerce_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_json_value(item) for item in value]
    if value is None:
        return None
    try:
        return _coerce_json_value(json.loads(str(value)))
    except Exception:
        return value


def _iter_resistance_mutations(mutations: object):
    mutations = _coerce_json_value(mutations)
    if not mutations:
        return

    if isinstance(mutations, dict):
        for drug, value in mutations.items():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield {
                            "drug": str(item.get("drug") or drug),
                            "mutation": str(item.get("mutation") or item.get("variant") or item.get("change") or item),
                            "gene": str(item.get("gene") or "n/a"),
                            "confidence": item.get("confidence") or item.get("support"),
                        }
                    else:
                        yield {"drug": str(drug), "mutation": str(item), "gene": "n/a", "confidence": None}
            elif isinstance(value, dict):
                yield {
                    "drug": str(value.get("drug") or drug),
                    "mutation": str(value.get("mutation") or value.get("variant") or value.get("change") or value),
                    "gene": str(value.get("gene") or "n/a"),
                    "confidence": value.get("confidence") or value.get("support"),
                }
            else:
                yield {"drug": str(drug), "mutation": str(value), "gene": "n/a", "confidence": None}
        return

    if isinstance(mutations, list):
        for item in mutations:
            if isinstance(item, dict):
                yield {
                    "drug": str(item.get("drug") or "n/a"),
                    "mutation": str(item.get("mutation") or item.get("variant") or item.get("change") or item),
                    "gene": str(item.get("gene") or "n/a"),
                    "confidence": item.get("confidence") or item.get("support"),
                }
            else:
                yield {"drug": "n/a", "mutation": str(item), "gene": "n/a", "confidence": None}


def _resistance_validation_record(
    *,
    sample_id: str,
    drug: str,
    gene: str,
    mutation: str,
    confidence: object,
    predicted_drug_resistance: object,
    catalogue: str | None,
) -> dict[str, Any]:
    mapping_status = _drug_gene_mapping_status(drug, gene)
    if mapping_status == "unusual_gene_drug_mapping":
        report_status = "suppressed"
        clinical_status = "do_not_report_mapping_error"
    elif mapping_status == "expected_gene":
        report_status = "not_validated"
        clinical_status = "requires_phenotypic_dst_confirmation"
    else:
        report_status = "not_validated"
        clinical_status = "requires_catalogue_review"

    return {
        "sample_id": sample_id,
        "drug": drug,
        "gene": gene,
        "mutation": mutation,
        "tool_confidence": confidence,
        "predicted_drug_resistance": _coerce_json_value(predicted_drug_resistance),
        "catalogue_version": catalogue,
        "mapping_status": mapping_status,
        "report_status": report_status,
        "clinical_status": clinical_status,
    }


def _generate_resistance_validation_artifact(catalogue: str | None = None) -> dict[str, Any]:
    db = SessionLocal()
    records: list[dict[str, Any]] = []
    try:
        rows = db.execute(
            text(
                """
                SELECT
                    sample_id::text AS sample_id,
                    resistance_mutations,
                    predicted_drug_resistance,
                    confidence_score
                FROM tb_interpretation
                WHERE resistance_mutations IS NOT NULL
                AND resistance_mutations::text NOT IN ('null', '{}', '[]')
                ORDER BY sample_id::text
                """
            )
        ).mappings().all()

        for row in rows:
            for mut in _iter_resistance_mutations(row.get("resistance_mutations")):
                records.append(
                    _resistance_validation_record(
                        sample_id=str(row.get("sample_id") or ""),
                        drug=str(mut.get("drug") or "n/a"),
                        gene=str(mut.get("gene") or "n/a"),
                        mutation=str(mut.get("mutation") or "n/a"),
                        confidence=mut.get("confidence") or row.get("confidence_score"),
                        predicted_drug_resistance=row.get("predicted_drug_resistance"),
                        catalogue=catalogue,
                    )
                )

        summary = {
            "total_mutation_calls": len(records),
            "expected_gene_calls": sum(1 for r in records if r["mapping_status"] == "expected_gene"),
            "unusual_gene_drug_mapping_calls": sum(1 for r in records if r["mapping_status"] == "unusual_gene_drug_mapping"),
            "unknown_mapping_calls": sum(1 for r in records if r["mapping_status"] == "unknown_catalogue_mapping"),
            "suppressed_calls": sum(1 for r in records if r["report_status"] == "suppressed"),
            "validated_calls": 0,
        }
        payload = {
            "generated_at": _iso_now(),
            "status": "completed" if records else "no_mutation_calls",
            "validation_scope": "local expected gene-drug mapping screen; clinical validation requires curated catalogue review and phenotypic DST",
            "catalogue_version": catalogue,
            "pipeline_validation_status": "not_validated_under_review",
            "summary": summary,
            "records": records,
        }
    except Exception as exc:
        payload = {
            "generated_at": _iso_now(),
            "status": "failed",
            "message": str(exc),
            "validation_scope": "local expected gene-drug mapping screen",
            "catalogue_version": catalogue,
            "pipeline_validation_status": "not_validated_under_review",
            "summary": {
                "total_mutation_calls": 0,
                "expected_gene_calls": 0,
                "unusual_gene_drug_mapping_calls": 0,
                "unknown_mapping_calls": 0,
                "suppressed_calls": 0,
                "validated_calls": 0,
            },
            "records": [],
        }
    finally:
        db.close()

    RESISTANCE_VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")
    return payload


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


def _run_tbprofiler_on_fasta(tbprofiler_exec: str, fasta_files: list[Path], sample_inputs: list[dict[str, Any]]) -> dict[str, Any]:
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

    output_jsons: list[str] = []
    failures: list[dict[str, Any]] = []
    successful_samples = 0

    with tempfile.TemporaryDirectory(prefix="tbprofiler_") as tmp_dir:
        tmp_base = Path(tmp_dir)
        for item in sample_inputs:
            sample_id = str(item["sample_id"])
            sequence = str(item["sequence"])
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

            started_at = datetime.now().timestamp() - 1.0
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
            if proc.returncode == 0 and _written_during_run(result_json, started_at):
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
        "input_fasta_files": [str(p.as_posix()) for p in fasta_files],
        "attempted_sample_ids": [str(item["sample_id"]) for item in sample_inputs],
        "successful_samples": successful_samples,
        "failed_samples": len(sample_inputs) - successful_samples,
        "output_jsons": output_jsons,
        "failures": failures[:10],
    }


def _run_tbprofiler_on_fasta_docker(fasta_files: list[Path], sample_inputs: list[dict[str, Any]], image: str) -> dict[str, Any]:
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

    output_jsons: list[str] = []
    failures: list[dict[str, Any]] = []
    successful_samples = 0

    with tempfile.TemporaryDirectory(prefix="tbprofiler_input_", dir=str(EXPORTS)) as tmp_dir:
        tmp_base = Path(tmp_dir)
        for item in sample_inputs:
            sample_id = str(item["sample_id"])
            sequence = str(item["sequence"])
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

            started_at = datetime.now().timestamp() - 1.0
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
            if proc.returncode == 0 and _written_during_run(result_json, started_at):
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
        "input_fasta_files": [str(p.as_posix()) for p in fasta_files],
        "attempted_sample_ids": [str(item["sample_id"]) for item in sample_inputs],
        "successful_samples": successful_samples,
        "failed_samples": len(sample_inputs) - successful_samples,
        "output_jsons": output_jsons,
        "failures": failures[:10],
    }


def _run_tbprofiler_on_fasta_wsl(fasta_files: list[Path], sample_inputs: list[dict[str, Any]], wsl_env: str) -> dict[str, Any]:
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
        for item in sample_inputs:
            sample_id = str(item["sample_id"])
            sequence = str(item["sequence"])
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
            started_at = datetime.now().timestamp() - 1.0
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
            if proc.returncode == 0 and _written_during_run(result_json, started_at):
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
        "input_fasta_files": [str(p.as_posix()) for p in fasta_files],
        "attempted_sample_ids": [str(item["sample_id"]) for item in sample_inputs],
        "successful_samples": successful_samples,
        "failed_samples": len(sample_inputs) - successful_samples,
        "output_jsons": output_jsons,
        "failures": failures[:10],
    }


def _run_mykrobe_on_fasta_wsl(fasta_files: list[Path], sample_inputs: list[dict[str, Any]], wsl_env: str) -> dict[str, Any]:
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
        for item in sample_inputs:
            sample_id = str(item["sample_id"])
            sequence = str(item["sequence"])
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
            started_at = datetime.now().timestamp() - 1.0
            proc = subprocess.run(
                ["wsl", "--", "bash", "-lc", bash_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=1800,
                cwd=str(ROOT),
                check=False,
            )

            if proc.returncode == 0 and _written_during_run(result_json, started_at):
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
        "input_fasta_files": [str(p.as_posix()) for p in fasta_files],
        "attempted_sample_ids": [str(item["sample_id"]) for item in sample_inputs],
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
    sample_map = _load_sample_id_map()

    try:
        for path_str in result_json_paths:
            path = Path(path_str)
            # Filename: <sample_id>_mykrobe.json
            sample_id = path.stem.replace("_mykrobe", "")

            case_sample_id = _resolve_case_sample_id(db, sample_id, sample_map)
            if not case_sample_id:
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
                    "sample_id": case_sample_id,
                    "species_confirmation": "M. tuberculosis complex",
                    "lineage": fields["lineage"],
                    "predicted_drug_resistance": json.dumps(fields["predicted_drug_resistance"]) if fields["predicted_drug_resistance"] is not None else "null",
                    "confidence_score": fields["confidence_score"],
                    "interpretation_summary": fields["interpretation_summary"],
                },
            )
            imported += 1

        db.commit()
        status, message = _import_status("mykrobe", imported, skipped)
        return {
            "status": status,
            "message": message,
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
    sample_map = _load_sample_id_map()

    try:
        for path_str in result_json_paths:
            path = Path(path_str)
            sample_id = path.stem.replace(".results", "")

            case_sample_id = _resolve_case_sample_id(db, sample_id, sample_map)
            if not case_sample_id:
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
                    "sample_id": case_sample_id,
                    "species_confirmation": "M. tuberculosis complex",
                    "lineage": fields["lineage"],
                    "predicted_drug_resistance": json.dumps(fields["predicted_drug_resistance"]) if fields["predicted_drug_resistance"] is not None else "null",
                    "confidence_score": fields["confidence_score"],
                    "interpretation_summary": fields["interpretation_summary"],
                },
            )
            imported += 1

        db.commit()
        status, message = _import_status("tb-profiler", imported, skipped)
        return {
            "status": status,
            "message": message,
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
    sample_map = _load_sample_id_map()

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

                case_sample_id = _resolve_case_sample_id(db, sample_id, sample_map)
                if not case_sample_id:
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
                        "sample_id": case_sample_id,
                        "species_confirmation": "M. tuberculosis complex",
                        "lineage": lineage,
                        "predicted_drug_resistance": json.dumps(resistance_obj) if resistance_obj is not None else "null",
                        "confidence_score": confidence_score,
                        "interpretation_summary": interpretation_summary,
                    },
                )
                imported += 1

        db.commit()
        status, message = _import_status("Lineage/DR CSV", imported, skipped)
        return {
            "status": status,
            "message": message,
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
    resistance_catalogue = os.getenv("TB_RESISTANCE_CATALOGUE", "WHO TB catalogue v2 (2023)")

    tbprofiler_exec = _which_many(["tb-profiler", "tb-profiler.exe", "tb_profiler", "tb-profiler-tools"])
    mykrobe_exec = _which_many(["mykrobe", "mykrobe.exe"])
    inputs = _discover_inputs()
    fasta_inputs = _discover_fasta_inputs()
    fasta_sample_inputs = _prepare_fasta_sample_inputs(fasta_inputs)
    fasta_metadata = _fasta_input_metadata(fasta_inputs, fasta_sample_inputs)
    fasta_validation = _validate_fasta_sample_inputs(fasta_sample_inputs)
    fasta_ready_for_tools = fasta_validation["status"] == "matched"
    inputs.update(fasta_metadata)
    inputs["sample_id_validation"] = fasta_validation

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

    if fasta_inputs and not fasta_ready_for_tools:
        blocked_message = fasta_validation["message"]
        tbprofiler_run["message"] = blocked_message
        tbprofiler_local_run["message"] = blocked_message
        tbprofiler_wsl_run["message"] = blocked_message
        tbprofiler_docker_run["message"] = blocked_message
        mykrobe_run["message"] = blocked_message
        mykrobe_wsl_run["message"] = blocked_message

    if tbprofiler["status"] == "installed" and fasta_inputs and fasta_ready_for_tools:
        tbprofiler_local_run = _run_tbprofiler_on_fasta(tbprofiler_exec, fasta_inputs, fasta_sample_inputs)
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
        and fasta_ready_for_tools
        and wsl.get("available")
        and wsl_tbprofiler.get("status") == "installed"
        and (
            tbprofiler_run["status"] in {"skipped", "failed"}
            or tbprofiler["status"] in {"not_installed", "installed_but_unusable"}
        )
    ):
        tbprofiler_wsl_run = _run_tbprofiler_on_fasta_wsl(fasta_inputs, fasta_sample_inputs, wsl_env_name)
        if tbprofiler_wsl_run["status"] == "completed":
            tbprofiler_run = tbprofiler_wsl_run
        elif tbprofiler_run["status"] == "skipped":
            tbprofiler_run = tbprofiler_wsl_run

    if (
        docker_fallback_enabled
        and fasta_inputs
        and fasta_ready_for_tools
        and docker.get("available")
        and docker.get("daemon_running")
        and (
            tbprofiler_run["status"] in {"skipped", "failed"}
            or tbprofiler["status"] in {"not_installed", "installed_but_unusable"}
        )
    ):
        tbprofiler_docker_run = _run_tbprofiler_on_fasta_docker(fasta_inputs, fasta_sample_inputs, docker_image)
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
        and fasta_ready_for_tools
        and wsl.get("available")
        and wsl_mykrobe.get("status") == "installed"
    ):
        mykrobe_wsl_run = _run_mykrobe_on_fasta_wsl(fasta_inputs, fasta_sample_inputs, wsl_env_name)
        mykrobe_run = mykrobe_wsl_run

    # Import order: Mykrobe first (fills base data), TBProfiler second (overwrites as authoritative).
    if mykrobe_run["status"] == "completed" and mykrobe_run["output_jsons"]:
        mykrobe_import = _import_mykrobe_results(mykrobe_run["output_jsons"])

    if tbprofiler_run["status"] == "completed" and tbprofiler_run["output_jsons"]:
        tbprofiler_import = _import_tbprofiler_results(tbprofiler_run["output_jsons"])

    resistance_validation = _generate_resistance_validation_artifact(resistance_catalogue)

    # Discordance check: flag samples where both tools ran but disagree on R/S.
    dr_concordance: list[dict[str, Any]] = []
    if tbprofiler_run["output_jsons"] and mykrobe_run["output_jsons"]:
        dr_concordance = _detect_dr_discordance(
            tbprofiler_run["output_jsons"], mykrobe_run["output_jsons"]
        )
        _log_concordance_to_audit(dr_concordance)

    ready_inputs = inputs["fastq_count"] > 0 or inputs["vcf_count"] > 0 or inputs["fasta_count"] > 0
    overall_status = "completed"
    if not ready_inputs:
        overall_status = "ready_missing_inputs"
    elif fasta_inputs and not fasta_ready_for_tools:
        overall_status = "blocked_sample_id_mismatch"
    elif tbprofiler["status"] not in {"installed", "installed_but_unusable"} and mykrobe["status"] != "installed":
        overall_status = "blocked_no_tools"
    elif tbprofiler["status"] == "installed_but_unusable" and mykrobe["status"] != "installed":
        overall_status = "blocked_tool_dependencies"
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
        "resistance_validation": {
            "status": resistance_validation.get("status"),
            "artifact": str(RESISTANCE_VALIDATION_JSON.as_posix()),
            "summary": resistance_validation.get("summary", {}),
            "pipeline_validation_status": resistance_validation.get("pipeline_validation_status"),
        },
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

    OUT_JSON.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")
    print(json.dumps(payload, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
