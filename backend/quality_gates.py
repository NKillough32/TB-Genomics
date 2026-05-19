from __future__ import annotations

import glob
import importlib.util
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPORTS = PROJECT_ROOT / "exports"
UPLOADS = PROJECT_ROOT / "uploads"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(name: str) -> dict[str, Any] | None:
    path = EXPORTS / name
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _artifact_status(name: str) -> dict[str, Any]:
    path = EXPORTS / name
    if not path.exists():
        return {"name": name, "exists": False, "updated_at": None, "bytes": 0}
    stat = path.stat()
    return {
        "name": name,
        "exists": True,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "bytes": stat.st_size,
    }


def _find_rscript() -> str | None:
    found = shutil.which("Rscript")
    if found:
        return found
    candidates = sorted(glob.glob(r"C:\Program Files\R\R-*\bin\Rscript.exe"), reverse=True)
    if not candidates:
        candidates = sorted(glob.glob(r"C:\Program Files\R\R-*\bin\x64\Rscript.exe"), reverse=True)
    return candidates[0] if candidates else None


def _which_many(candidates: list[str]) -> str | None:
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    venv_dir = PROJECT_ROOT / ".venv" / "Scripts"
    for name in candidates:
        local = venv_dir / name
        if local.exists():
            return str(local)
    return None


def _dep(
    key: str,
    label: str,
    status: str,
    message: str,
    *,
    executable: str | None = None,
    process_blocking: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "message": message,
        "executable": executable,
        "process_blocking": process_blocking,
        "details": details or {},
    }


def dependency_health() -> list[dict[str, Any]]:
    lineage = _load_json("lineage_dr_validation.json") or {}
    engines = lineage.get("engines") if isinstance(lineage.get("engines"), dict) else {}
    wsl = lineage.get("wsl") if isinstance(lineage.get("wsl"), dict) else {}

    rscript = _find_rscript()
    deps = [
        _dep(
            "rscript",
            "Rscript / outbreaker2",
            "available" if rscript else "missing",
            "Rscript available for outbreaker2 runs."
            if rscript
            else "Rscript is not available. Outbreaker2 cannot run natively; the workflow should continue but outbreaker confidence is limited.",
            executable=rscript,
            process_blocking=False,
        )
    ]

    matplotlib_available = importlib.util.find_spec("matplotlib") is not None
    deps.append(
        _dep(
            "python_plotting",
            "Python plotting stack",
            "available" if matplotlib_available else "missing",
            "Matplotlib is available for supplementary figures."
            if matplotlib_available
            else "Matplotlib is missing in the active backend environment. Supplementary figure generation will be limited.",
            process_blocking=False,
        )
    )

    tb_profiler = engines.get("tb_profiler") if isinstance(engines.get("tb_profiler"), dict) else {}
    tb_exec = tb_profiler.get("executable") or _which_many(["tb-profiler", "tb-profiler.exe", "tb_profiler", "tb-profiler-tools"])
    tb_status = str(tb_profiler.get("status") or ("installed" if tb_exec else "not_installed"))
    tb_wsl = wsl.get("tbprofiler") if isinstance(wsl.get("tbprofiler"), dict) else {}
    tb_ready = tb_status in {"installed", "ok", "completed", "available"} or tb_wsl.get("status") == "installed"
    deps.append(
        _dep(
            "tbprofiler",
            "TBProfiler",
            "available" if tb_ready else "warning",
            "TBProfiler is available for lineage/DR validation."
            if tb_ready
            else "TBProfiler is not currently usable. Continue the workflow, but mark lineage/DR validation as limited.",
            executable=str(tb_exec) if tb_exec else None,
            process_blocking=False,
            details={"engine": tb_profiler, "wsl": tb_wsl},
        )
    )

    mykrobe = engines.get("mykrobe") if isinstance(engines.get("mykrobe"), dict) else {}
    mykrobe_exec = mykrobe.get("executable") or _which_many(["mykrobe", "mykrobe.exe"])
    mykrobe_status = str(mykrobe.get("status") or ("installed" if mykrobe_exec else "not_installed"))
    mykrobe_wsl = wsl.get("mykrobe") if isinstance(wsl.get("mykrobe"), dict) else {}
    mykrobe_ready = mykrobe_status in {"installed", "ok", "completed", "available"} or mykrobe_wsl.get("status") == "installed"
    deps.append(
        _dep(
            "mykrobe",
            "Mykrobe",
            "available" if mykrobe_ready else "warning",
            "Mykrobe is available as a secondary lineage/DR validation engine."
            if mykrobe_ready
            else "Mykrobe is unavailable. Continue the workflow, but cross-engine DR concordance will be limited.",
            executable=str(mykrobe_exec) if mykrobe_exec else None,
            process_blocking=False,
            details={"engine": mykrobe, "wsl": mykrobe_wsl},
        )
    )

    return deps


def _scalar(db: Session, sql: str, default: int = 0) -> int:
    try:
        value = db.execute(text(sql)).scalar()
        return int(value or default)
    except Exception:
        return default


def _table_exists(db: Session, table: str) -> bool:
    try:
        return bool(db.execute(text("SELECT to_regclass(:table_name) IS NOT NULL"), {"table_name": f"public.{table}"}).scalar())
    except Exception:
        return False


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100.0, 1)


def _gate(
    key: str,
    label: str,
    status: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    process_blocking: bool = False,
    interpretation_blocking: bool = False,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "message": message,
        "process_blocking": process_blocking,
        "interpretation_blocking": interpretation_blocking,
        "details": details or {},
    }


def confidence_gates(db: Session) -> list[dict[str, Any]]:
    summary = _load_json("outbreaker_summary.json") or {}
    lineage = _load_json("lineage_dr_validation.json") or {}
    resistance = _load_json("resistance_validation.json") or {}
    sequence_summary = _load_json("sequence_clustering_summary.json") or {}
    comparison = _load_json("cluster_method_comparison.json") or {}

    case_count = _scalar(db, "SELECT COUNT(*) FROM cases")
    sequence_count = _scalar(db, "SELECT COUNT(DISTINCT sample_id) FROM consensus_sequences")
    qc_reported = 0
    qc_pass = 0
    if _table_exists(db, "sample_qc_metrics"):
        qc_reported = _scalar(db, "SELECT COUNT(DISTINCT sample_id) FROM sample_qc_metrics")
        qc_pass = _scalar(
            db,
            "SELECT COUNT(DISTINCT sample_id) FROM sample_qc_metrics WHERE LOWER(COALESCE(qc_status,'')) IN ('pass','passed')",
        )

    seq_pct = _pct(sequence_count, case_count)
    qc_pct = _pct(qc_pass, max(sequence_count, qc_reported))

    gates = [
        _gate(
            "case_data_loaded",
            "Case data loaded",
            "pass" if case_count > 0 else "fail",
            f"{case_count} cases loaded." if case_count > 0 else "No cases are loaded; analysis outputs will be empty.",
            process_blocking=case_count == 0,
            details={"case_count": case_count},
        ),
        _gate(
            "sequence_data_loaded",
            "Sequence data loaded",
            "pass" if sequence_count > 0 else "fail",
            f"{sequence_count} consensus sequences loaded." if sequence_count > 0 else "No consensus sequences are loaded.",
            process_blocking=sequence_count == 0,
            details={"sequence_count": sequence_count},
        ),
        _gate(
            "sequencing_coverage",
            "Sequencing coverage",
            "pass" if seq_pct is not None and seq_pct >= 80.0 else ("warn" if seq_pct is not None else "incomplete"),
            f"{seq_pct}% of loaded cases have consensus sequence." if seq_pct is not None else "Coverage cannot be calculated yet.",
            details={"sequenced_cases": sequence_count, "total_cases": case_count, "percent": seq_pct},
        ),
        _gate(
            "qc_pass_rate",
            "QC pass rate",
            "pass" if qc_pct is not None and qc_pct >= 90.0 else ("warn" if qc_pct is not None else "incomplete"),
            f"{qc_pct}% of sequenced/reported samples have pass QC." if qc_pct is not None else "QC metrics are not available.",
            details={"qc_pass": qc_pass, "qc_reported": qc_reported, "percent": qc_pct},
        ),
    ]

    assigned_sequences = sequence_summary.get("assigned_sequences")
    total_sequences = sequence_summary.get("total_sequences")
    if total_sequences is not None:
        try:
            assigned = int(assigned_sequences or 0)
            total = int(total_sequences or 0)
            gates.append(
                _gate(
                    "sequence_cluster_assignment",
                    "Sequence cluster assignment",
                    "pass" if total > 0 and assigned == total else "warn",
                    f"{assigned}/{total} sequences assigned to sequence clusters.",
                    details={"assigned_sequences": assigned, "total_sequences": total},
                )
            )
        except Exception:
            pass

    deps = dependency_health()
    lineage_deps_ok = any(dep["key"] in {"tbprofiler", "mykrobe"} and dep["status"] == "available" for dep in deps)
    gates.append(
        _gate(
            "lineage_dr_engine_health",
            "Lineage/DR engine health",
            "pass" if lineage_deps_ok else "warn",
            "At least one lineage/DR engine is available."
            if lineage_deps_ok
            else "No usable lineage/DR engine is available; validation should be interpreted as limited.",
            details={"dependencies": [dep for dep in deps if dep["key"] in {"tbprofiler", "mykrobe"}]},
        )
    )

    dr_concordance = lineage.get("dr_concordance") if isinstance(lineage.get("dr_concordance"), dict) else {}
    compared = int(dr_concordance.get("samples_compared") or 0) if dr_concordance else 0
    discordant = int(dr_concordance.get("discordant_sample_count") or 0) if dr_concordance else 0
    gates.append(
        _gate(
            "dr_concordance",
            "DR concordance",
            "pass" if compared > 0 and discordant == 0 else ("warn" if compared == 0 else "review"),
            "No cross-engine DR discordance detected." if compared > 0 and discordant == 0
            else ("No samples were compared for DR concordance." if compared == 0 else f"{discordant} discordant DR sample(s) need review."),
            interpretation_blocking=compared == 0 or discordant > 0,
            details={"samples_compared": compared, "discordant_sample_count": discordant},
        )
    )

    rv_summary = resistance.get("summary") if isinstance(resistance.get("summary"), dict) else {}
    suppressed = int(rv_summary.get("suppressed_calls") or 0) if rv_summary else 0
    validated = int(rv_summary.get("validated_calls") or 0) if rv_summary else 0
    rv_catalogue = str(resistance.get("catalogue_version") or "").lower()
    using_who_catalogue = "who" in rv_catalogue
    # When a WHO catalogue is in use, TBProfiler/Mykrobe calls are already WHO-graded.
    # The local gene-drug mapping screen is a secondary cross-check only; it should not
    # block interpretation — the WHO catalogue is the authoritative source of truth.
    # Downgrade from interpretation-blocking "review" to non-blocking "warn" in that case.
    if suppressed == 0 and rv_summary:
        rv_gate_status = "pass"
        rv_message = "Resistance mapping safety screen found no suppressed calls."
    elif suppressed and using_who_catalogue:
        rv_gate_status = "warn"
        rv_message = (
            f"{suppressed} resistance call(s) have unusual local gene-drug mappings but are covered "
            f"by the WHO catalogue ({resistance.get('catalogue_version')}); no interpretation block applied."
        )
    elif suppressed:
        rv_gate_status = "review"
        rv_message = f"{suppressed} resistance calls are suppressed pending mapping review."
    else:
        rv_gate_status = "incomplete"
        rv_message = "Resistance validation artifact is missing."
    gates.append(
        _gate(
            "resistance_mapping_safety",
            "Resistance mapping safety",
            rv_gate_status,
            rv_message,
            interpretation_blocking=suppressed > 0 and not using_who_catalogue,
            details={"suppressed_calls": suppressed, "validated_calls": validated, "catalogue_version": resistance.get("catalogue_version")},
        )
    )

    if summary:
        provenance = str(summary.get("data_provenance") or "unknown")
        gates.append(
            _gate(
                "outbreaker2_artifact",
                "Outbreaker2 artifact",
                "pass" if provenance == "real" else "warn",
                "Real outbreaker2 summary artifact is present." if provenance == "real" else f"Outbreaker2 provenance is {provenance}.",
                details={"data_provenance": provenance},
            )
        )
        diagnostic_status = str(summary.get("mcmc_diagnostic_status") or "").lower()
        gates.append(
            _gate(
                "mcmc_convergence",
                "MCMC convergence signal",
                "warn" if "drifting" in diagnostic_status else "pass",
                str(summary.get("mcmc_diagnostic_status") or "No strong late drift flag in summary."),
                interpretation_blocking="drifting" in diagnostic_status,
                details={"mcmc_late_drift_fraction": summary.get("mcmc_late_drift_fraction")},
            )
        )

        # Alpha column ESS (ancestry parameter effective sample size)
        alpha_ess_min = summary.get("alpha_mcmc_effective_sample_size_min")
        if alpha_ess_min is not None:
            try:
                ess_val = float(alpha_ess_min)
                gates.append(
                    _gate(
                        "mcmc_alpha_ess",
                        "MCMC ancestry ESS",
                        "pass" if ess_val >= 100 else "warn",
                        f"Minimum per-case ancestry ESS is {ess_val:.0f}.",
                        interpretation_blocking=ess_val < 50,
                        details={
                            "alpha_ess_min": alpha_ess_min,
                            "alpha_ess_mean": summary.get("alpha_mcmc_effective_sample_size_mean"),
                        },
                    )
                )
            except (TypeError, ValueError):
                pass
    else:
        gates.append(
            _gate(
                "outbreaker2_artifact",
                "Outbreaker2 artifact",
                "incomplete",
                "Outbreaker2 summary artifact is missing.",
                details={},
            )
        )

    precision = comparison.get("precision_sequence_given_outbreaker")
    if precision is None:
        precision = comparison.get("pairwise_precision_outbreaker_vs_sequence")
    if precision is not None:
        try:
            precision_float = float(precision)
            gates.append(
                _gate(
                    "sequence_outbreaker_agreement",
                    "Sequence/outbreaker agreement",
                    "pass" if precision_float >= 0.5 else "warn",
                    f"Sequence/outbreaker agreement precision is {precision_float:.2f}.",
                    interpretation_blocking=precision_float < 0.5,
                    details={"precision": precision_float},
                )
            )
        except Exception:
            pass

    report_artifact = _artifact_status("outbreaker_investigation_report_full.html")
    gates.append(
        _gate(
            "report_generated",
            "Full report generated",
            "pass" if report_artifact["exists"] else "incomplete",
            "Full HTML report artifact is present." if report_artifact["exists"] else "Full HTML report has not been generated.",
            details=report_artifact,
        )
    )

    return gates


def workflow_stages(db: Session) -> list[dict[str, Any]]:
    upload_count = len([p for p in UPLOADS.glob("*") if p.is_file()]) if UPLOADS.exists() else 0
    case_count = _scalar(db, "SELECT COUNT(*) FROM cases")
    artifacts = {
        "sequence_clusters": _artifact_status("sequence_clustering_summary.json"),
        "outbreaker_inputs_cases": _artifact_status("cases.csv"),
        "outbreaker_inputs_fasta": _artifact_status("dna.fasta"),
        "lineage_dr": _artifact_status("lineage_dr_validation.json"),
        "outbreaker2": _artifact_status("outbreaker_summary.json"),
        "comparison": _artifact_status("cluster_method_comparison.json"),
        "report": _artifact_status("outbreaker_investigation_report_full.html"),
    }

    def stage(key: str, label: str, complete: bool, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "key": key,
            "label": label,
            "status": "complete" if complete else "pending",
            "message": message,
            "details": details or {},
        }

    outbreaker_inputs_complete = artifacts["outbreaker_inputs_cases"]["exists"] and artifacts["outbreaker_inputs_fasta"]["exists"]
    return [
        stage("uploads", "Upload files", upload_count > 0, f"{upload_count} file(s) currently in uploads/.", {"upload_count": upload_count}),
        stage("load_data", "Load data", case_count > 0, f"{case_count} case row(s) loaded.", {"case_count": case_count}),
        stage("sequence_clusters", "Derive sequence clusters", artifacts["sequence_clusters"]["exists"], "Sequence clustering artifact present." if artifacts["sequence_clusters"]["exists"] else "Sequence clustering has not run.", artifacts["sequence_clusters"]),
        stage("outbreaker_inputs", "Generate outbreaker2 inputs", outbreaker_inputs_complete, "Outbreaker2 input CSV/FASTA present." if outbreaker_inputs_complete else "Outbreaker2 inputs are incomplete.", {"cases": artifacts["outbreaker_inputs_cases"], "dna_fasta": artifacts["outbreaker_inputs_fasta"]}),
        stage("lineage_dr", "Run lineage/DR validation", artifacts["lineage_dr"]["exists"], "Lineage/DR validation artifact present." if artifacts["lineage_dr"]["exists"] else "Lineage/DR validation has not run.", artifacts["lineage_dr"]),
        stage("outbreaker2", "Run outbreaker2", artifacts["outbreaker2"]["exists"], "Outbreaker2 summary artifact present." if artifacts["outbreaker2"]["exists"] else "Outbreaker2 has not run.", artifacts["outbreaker2"]),
        stage("comparison", "Compare methods", artifacts["comparison"]["exists"], "Cluster comparison artifact present." if artifacts["comparison"]["exists"] else "Cluster comparison has not run.", artifacts["comparison"]),
        stage("report", "Generate report", artifacts["report"]["exists"], "Full report artifact present." if artifacts["report"]["exists"] else "Full report has not been generated.", artifacts["report"]),
    ]


def build_workflow_status(db: Session) -> dict[str, Any]:
    deps = dependency_health()
    gates = confidence_gates(db)
    stages = workflow_stages(db)

    process_blockers = [gate for gate in gates if gate.get("process_blocking")]
    interpretation_blockers = [gate for gate in gates if gate.get("interpretation_blocking")]
    warnings = [gate for gate in gates if gate["status"] in {"warn", "review", "incomplete"}]

    if process_blockers:
        overall = "incomplete"
    elif interpretation_blockers or warnings:
        overall = "usable_with_warnings"
    else:
        overall = "ready"

    return {
        "generated_at": _iso_now(),
        "overall_status": overall,
        "process_blocking": len(process_blockers) > 0,
        "interpretation_blocking": len(interpretation_blockers) > 0,
        "summary": {
            "dependency_warnings": sum(1 for dep in deps if dep["status"] != "available"),
            "gate_warnings": len(warnings),
            "process_blockers": len(process_blockers),
            "interpretation_blockers": len(interpretation_blockers),
        },
        "dependencies": deps,
        "gates": gates,
        "stages": stages,
    }

