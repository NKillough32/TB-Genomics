import os
import json
import re
import csv
import hashlib
import logging
import base64
import html as html_lib
from pathlib import Path
from statistics import median
from itertools import combinations

from datetime import datetime
from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from backend.database import SessionLocal
from backend.models import Case
from backend.data_safety import enforce_operational_dataset, get_data_safety_status

router = APIRouter(prefix="/cases", tags=["cases"])
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
logger = logging.getLogger(__name__)


def _export_path(*parts: str) -> str:
    """Return an absolute path under the repository export directory."""
    return os.path.join(PROJECT_ROOT, "exports", *parts)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _lineage_analysis_summary(db: Session) -> dict:
    """Return live lineage/DR interpretation counts from tb_interpretation."""
    summary = {
        "interpreted_samples": 0,
        "samples_with_lineage": 0,
        "samples_with_resistance_calls": 0,
        "samples_with_interpretation_summary": 0,
    }

    try:
        summary_row = db.execute(
            text(
                """
                SELECT
                    COUNT(*)::int AS interpreted_samples,
                    COUNT(*) FILTER (WHERE lineage IS NOT NULL AND BTRIM(lineage) <> '')::int AS samples_with_lineage,
                    COUNT(*) FILTER (
                        WHERE predicted_drug_resistance IS NOT NULL
                        AND predicted_drug_resistance::text NOT IN ('null', '{}', '[]')
                    )::int AS samples_with_resistance_calls,
                    COUNT(*) FILTER (
                        WHERE interpretation_summary IS NOT NULL AND BTRIM(interpretation_summary) <> ''
                    )::int AS samples_with_interpretation_summary
                FROM tb_interpretation
                """
            )
        ).mappings().first()
        if summary_row:
            summary = {
                "interpreted_samples": int(summary_row["interpreted_samples"] or 0),
                "samples_with_lineage": int(summary_row["samples_with_lineage"] or 0),
                "samples_with_resistance_calls": int(summary_row["samples_with_resistance_calls"] or 0),
                "samples_with_interpretation_summary": int(summary_row["samples_with_interpretation_summary"] or 0),
            }
    except Exception as exc:
        summary["error"] = str(exc)

    return summary


def _lineage_epi_summary(db: Session) -> dict:
    """Return epidemiology-oriented lineage/DR indicators for decision support."""
    summary = {
        "samples_with_any_resistance_signal": 0,
        "rifampicin_resistant_suspected": 0,
        "isoniazid_resistant_suspected": 0,
        "mdr_suspected": 0,
        "fluoroquinolone_resistant_suspected": 0,
        "top_lineages": [],
        "top_lineage_region_pairs": [],
    }

    try:
        base_row = db.execute(
            text(
                """
                WITH dr AS (
                    SELECT LOWER(CAST(predicted_drug_resistance AS TEXT)) AS dr_text
                    FROM tb_interpretation
                    WHERE predicted_drug_resistance IS NOT NULL
                    AND predicted_drug_resistance::text NOT IN ('null', '{}', '[]')
                )
                SELECT
                    COUNT(*) FILTER (
                        WHERE dr_text LIKE '%resistant%'
                        OR dr_text LIKE '%\"r\"%'
                    )::int AS samples_with_any_resistance_signal,
                    COUNT(*) FILTER (
                        WHERE dr_text LIKE '%rifamp%'
                        AND (dr_text LIKE '%resistant%' OR dr_text LIKE '%\"r\"%')
                    )::int AS rifampicin_resistant_suspected,
                    COUNT(*) FILTER (
                        WHERE dr_text LIKE '%isoniazid%'
                        AND (dr_text LIKE '%resistant%' OR dr_text LIKE '%\"r\"%')
                    )::int AS isoniazid_resistant_suspected,
                    COUNT(*) FILTER (
                        WHERE dr_text LIKE '%fluoro%'
                        AND (dr_text LIKE '%resistant%' OR dr_text LIKE '%\"r\"%')
                    )::int AS fluoroquinolone_resistant_suspected,
                    COUNT(*) FILTER (
                        WHERE dr_text LIKE '%rifamp%'
                        AND dr_text LIKE '%isoniazid%'
                        AND (dr_text LIKE '%resistant%' OR dr_text LIKE '%\"r\"%')
                    )::int AS mdr_suspected
                FROM dr
                """
            )
        ).mappings().first()

        if base_row:
            summary.update(
                {
                    "samples_with_any_resistance_signal": int(base_row["samples_with_any_resistance_signal"] or 0),
                    "rifampicin_resistant_suspected": int(base_row["rifampicin_resistant_suspected"] or 0),
                    "isoniazid_resistant_suspected": int(base_row["isoniazid_resistant_suspected"] or 0),
                    "mdr_suspected": int(base_row["mdr_suspected"] or 0),
                    "fluoroquinolone_resistant_suspected": int(base_row["fluoroquinolone_resistant_suspected"] or 0),
                }
            )

        lineage_rows = db.execute(
            text(
                """
                SELECT lineage, COUNT(*)::int AS n
                FROM tb_interpretation
                WHERE lineage IS NOT NULL AND BTRIM(lineage) <> ''
                GROUP BY lineage
                ORDER BY n DESC, lineage
                LIMIT 5
                """
            )
        ).mappings().all()
        summary["top_lineages"] = [
            {"lineage": str(r["lineage"]), "count": int(r["n"] or 0)}
            for r in lineage_rows
        ]

        pair_rows = db.execute(
            text(
                """
                SELECT c.geographic_region AS region, ti.lineage, COUNT(*)::int AS n
                FROM tb_interpretation ti
                JOIN cases c ON c.pseudonymised_case_id = ti.sample_id
                WHERE ti.lineage IS NOT NULL AND BTRIM(ti.lineage) <> ''
                GROUP BY c.geographic_region, ti.lineage
                ORDER BY n DESC, c.geographic_region, ti.lineage
                LIMIT 8
                """
            )
        ).mappings().all()
        summary["top_lineage_region_pairs"] = [
            {
                "region": str(r["region"]),
                "lineage": str(r["lineage"]),
                "count": int(r["n"] or 0),
            }
            for r in pair_rows
        ]
    except Exception as exc:
        summary["error"] = str(exc)

    return summary


def _derive_effective_engine_status(payload: dict) -> dict:
    """Compute user-facing engine statuses from local + fallback execution context."""
    payload = payload or {}
    engines = payload.get("engines") or {}
    tb_local = (engines.get("tb_profiler") or {}).get("status", "unknown")
    mykrobe_local = (engines.get("mykrobe") or {}).get("status", "unknown")

    tb_run = payload.get("tbprofiler_run") or {}
    tb_wsl_run = payload.get("tbprofiler_wsl_run") or {}
    tb_docker_run = payload.get("tbprofiler_docker_run") or {}

    mykrobe_run = payload.get("mykrobe_run") or {}
    mykrobe_wsl_run = payload.get("mykrobe_wsl_run") or {}

    wsl_info = payload.get("wsl") or {}
    wsl_tb = (wsl_info.get("tbprofiler") or {}).get("status", "unknown")
    wsl_mykrobe = (wsl_info.get("mykrobe") or {}).get("status", "unknown")

    tb_effective = tb_local
    if tb_run.get("status") == "completed":
        tb_effective = f"available_via_{tb_run.get('runner', 'runner')}"
    elif tb_wsl_run.get("status") == "completed" or wsl_tb == "installed":
        tb_effective = "available_via_wsl"
    elif tb_docker_run.get("status") == "completed":
        tb_effective = "available_via_docker"

    mykrobe_effective = mykrobe_local
    if mykrobe_run.get("status") == "completed":
        mykrobe_effective = f"available_via_{mykrobe_run.get('runner', 'runner')}"
    elif mykrobe_wsl_run.get("status") == "completed" or wsl_mykrobe == "installed":
        mykrobe_effective = "available_via_wsl"

    return {
        "tb_profiler": tb_effective,
        "mykrobe": mykrobe_effective,
        "tb_profiler_local": tb_local,
        "mykrobe_local": mykrobe_local,
    }


def _secondary_epi_summary(
    secondary_validation_data: dict | None,
    transmission_data: dict | None,
    method_comparison_data: dict | None,
) -> dict:
    """Summarize usable epidemiology from secondary-validation context."""
    secondary_validation_data = secondary_validation_data or {}
    transmission_data = transmission_data or {}
    method_comparison_data = method_comparison_data or {}

    prerequisites = secondary_validation_data.get("prerequisites") or {}
    consensus = secondary_validation_data.get("consensus") or {}
    agreement = method_comparison_data.get("agreement") or {}

    high_confidence_edges = int(transmission_data.get("high_confidence_edges") or 0)
    key_nodes = transmission_data.get("key_nodes") or []

    return {
        "consensus_state": str(consensus.get("status") or "unknown"),
        "tree_input_available": bool(prerequisites.get("has_tree_newick")),
        "cases_input_available": bool(prerequisites.get("has_cases_csv")),
        "primary_network_available": bool(transmission_data),
        "high_confidence_links": high_confidence_edges,
        "priority_nodes_flagged": len(key_nodes),
        "pairwise_precision": round(float(agreement.get("pairwise_precision_outbreaker_vs_sequence") or 0.0), 3),
        "pairwise_recall": round(float(agreement.get("pairwise_recall_outbreaker_vs_sequence") or 0.0), 3),
        "pairwise_jaccard": round(float(agreement.get("pairwise_jaccard") or 0.0), 3),
    }


def _short_case_id(case_id: str | None) -> str:
    if not case_id:
        return "n/a"
    return str(case_id)[:8]


def _resistance_profile_text(predicted_dr: object) -> str:
    """Render concise drug-resistance profile from JSON-like field."""
    if not predicted_dr:
        return "none"

    if isinstance(predicted_dr, str):
        return predicted_dr[:64]

    if isinstance(predicted_dr, dict):
        resistant = []
        for drug, phenotype in predicted_dr.items():
            phen = str(phenotype).lower()
            if "resistant" in phen or phen == "r":
                resistant.append(str(drug))
        return ", ".join(resistant[:4]) if resistant else "susceptible"

    return str(predicted_dr)[:64]


def _iter_resistance_mutations(mutations: object):
    """Yield normalized mutation rows from flexible JSON structures."""
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
                            "confidence": str(item.get("confidence") or item.get("support") or "n/a"),
                        }
                    else:
                        yield {
                            "drug": str(drug),
                            "mutation": str(item),
                            "gene": "n/a",
                            "confidence": "n/a",
                        }
            elif isinstance(value, dict):
                yield {
                    "drug": str(value.get("drug") or drug),
                    "mutation": str(value.get("mutation") or value.get("variant") or value.get("change") or value),
                    "gene": str(value.get("gene") or "n/a"),
                    "confidence": str(value.get("confidence") or value.get("support") or "n/a"),
                }
            else:
                yield {
                    "drug": str(drug),
                    "mutation": str(value),
                    "gene": "n/a",
                    "confidence": "n/a",
                }
        return

    if isinstance(mutations, list):
        for item in mutations:
            if isinstance(item, dict):
                yield {
                    "drug": str(item.get("drug") or "n/a"),
                    "mutation": str(item.get("mutation") or item.get("variant") or item.get("change") or item),
                    "gene": str(item.get("gene") or "n/a"),
                    "confidence": str(item.get("confidence") or item.get("support") or "n/a"),
                }
            else:
                yield {
                    "drug": "n/a",
                    "mutation": str(item),
                    "gene": "n/a",
                    "confidence": "n/a",
                }


def _snp_distance(seq_a: str, seq_b: str) -> int | None:
    if not seq_a or not seq_b:
        return None

    a = str(seq_a).upper()
    b = str(seq_b).upper()
    common = min(len(a), len(b))
    mismatches = sum(1 for i in range(common) if a[i] != b[i])
    return mismatches + abs(len(a) - len(b))


def _normalise_token(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _expected_genes_for_drug(drug: object) -> list[str]:
    """Return the WHO TB Mutation Catalogue v2 expected resistance genes for a given drug.

    Ordered from most-specific to least-specific substring so that e.g.
    "rifabutin" is matched before the broader "rifamp" prefix.
    Gene names are case-sensitive per standard TB nomenclature.
    """
    drug_text = str(drug or "").lower()
    # (substring_marker, [expected_genes])  — first match wins
    mapping = [
        # ── Rifamycins ──────────────────────────────────────────────────────
        ("rifabutin",        ["rpoB"]),
        ("rifapentine",      ["rpoB"]),
        ("rifamp",           ["rpoB"]),                          # rifampicin / rifampin
        # ── Isoniazid ───────────────────────────────────────────────────────
        ("isoniazid",        ["katG", "inhA", "fabG1", "ahpC", "kasA"]),
        # ── Ethionamide / Prothionamide (share inhA/fabG1 with isoniazid) ──
        ("prothionamide",    ["ethA", "ethR", "inhA", "fabG1", "mshA"]),
        ("ethionamide",      ["ethA", "ethR", "inhA", "fabG1", "mshA"]),
        # ── Pyrazinamide ────────────────────────────────────────────────────
        ("pyrazinamide",     ["pncA", "rpsA", "panD"]),
        # ── Ethambutol ──────────────────────────────────────────────────────
        ("ethambutol",       ["embB", "embA", "embC", "embR", "iniB"]),
        # ── Fluoroquinolones (individual agents before generic class) ───────
        ("moxifloxacin",     ["gyrA", "gyrB"]),
        ("levofloxacin",     ["gyrA", "gyrB"]),
        ("ciprofloxacin",    ["gyrA", "gyrB"]),
        ("ofloxacin",        ["gyrA", "gyrB"]),
        ("gatifloxacin",     ["gyrA", "gyrB"]),
        ("fluoroquinolone",  ["gyrA", "gyrB"]),                  # generic class
        ("fluoroquin",       ["gyrA", "gyrB"]),                  # abbreviation
        # ── Aminoglycosides / injectable second-line agents ─────────────────
        ("amikacin",         ["rrs", "eis"]),
        ("kanamycin",        ["rrs", "eis"]),
        ("capreomycin",      ["rrs", "tlyA"]),
        ("streptomycin",     ["rpsL", "rrs", "gid"]),
        # ── Bedaquiline ─────────────────────────────────────────────────────
        ("bedaquiline",      ["atpE", "Rv0678", "pepQ", "mmpL5", "mmpS5"]),
        # ── Linezolid ───────────────────────────────────────────────────────
        ("linezolid",        ["rrl", "rplC"]),
        # ── Clofazimine ─────────────────────────────────────────────────────
        ("clofazimine",      ["Rv0678", "pepQ", "mmpL5", "mmpS5"]),
        # ── Delamanid ───────────────────────────────────────────────────────
        ("delamanid",        ["ddn", "fgd1", "fbiA", "fbiB", "fbiC"]),
        # ── Pretomanid ──────────────────────────────────────────────────────
        ("pretomanid",       ["ddn", "fgd1", "fbiA", "fbiB", "fbiC", "Rv3547"]),
        # ── Para-aminosalicylic acid (PAS) ───────────────────────────────────
        ("aminosalicylic",   ["thyA", "folC", "thyX"]),
        ("para-amino",       ["thyA", "folC", "thyX"]),
        # ── Cycloserine / Terizidone ─────────────────────────────────────────
        ("terizidone",       ["ald", "alr"]),
        ("cycloserine",      ["ald", "alr"]),
        # ── Carbapenems (used in BPaL regimens) ─────────────────────────────
        ("imipenem",         ["blaC"]),
        ("meropenem",        ["blaC"]),
        # ── Clavam ──────────────────────────────────────────────────────────
        ("clavulanate",      ["blaC"]),
    ]
    for marker, genes in mapping:
        if marker in drug_text:
            return genes
    return []


def _drug_gene_compatibility(drug: object, gene: object) -> str:
    expected = _expected_genes_for_drug(drug)
    gene_text = _normalise_token(gene)
    if not expected or not gene_text or gene_text == "na":
        return "not_available"
    if any(_normalise_token(expected_gene) in gene_text for expected_gene in expected):
        return "compatible"
    return "check_catalogue"


def _drug_gene_status_label(drug: object, gene: object) -> str:
    """Human-readable status for operational resistance interpretation."""
    compatibility = _drug_gene_compatibility(drug, gene)
    if compatibility == "compatible":
        return "Valid expected gene"
    if compatibility == "check_catalogue":
        return "Unusual gene-drug mapping"
    return "Unknown / catalogue not available"


def _pair_key(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((str(left), str(right))))


def _pairwise_matrix(sequence_by_case: dict[str, str]) -> dict[tuple[str, str], int]:
    matrix: dict[tuple[str, str], int] = {}
    sequenced_cases = sorted(case_id for case_id, seq in sequence_by_case.items() if seq)
    for left, right in combinations(sequenced_cases, 2):
        dist = _snp_distance(sequence_by_case[left], sequence_by_case[right])
        if dist is not None:
            matrix[_pair_key(left, right)] = int(dist)
    return matrix


def _sha256_of_file(path: str) -> str:
    if not path or not os.path.exists(path) or not os.path.isfile(path):
        return "n/a"
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(8192), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return "n/a"


def _write_csv_rows(path: str, rows: list[list[object]]) -> None:
    """Write UTF-8 CSV rows for appendix companion files without aborting report generation."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            for row in rows:
                writer.writerow(["" if value is None else str(value) for value in row])
    except Exception:
        logger.exception("Unable to write companion CSV export to %s", path)


def _confidence_tier(
    *,
    qc_status: str,
    contamination_flag: bool,
    pairwise_distance: int | None,
    outbreaker_probability: float | None,
    same_cluster: bool,
) -> str:
    qc_ok = str(qc_status or "").lower() in ("pass", "passed") and not contamination_flag
    if not qc_ok:
        return "Exploratory"
    if pairwise_distance is not None and pairwise_distance <= 5 and same_cluster:
        return "High confidence"
    if pairwise_distance is not None and pairwise_distance <= 12 and same_cluster:
        return "Moderate confidence"
    if outbreaker_probability is not None and outbreaker_probability >= 0.70 and same_cluster:
        return "Moderate confidence"
    return "Exploratory"


def _format_table_caption(caption_text: str, table_number: int) -> str:
    raw_caption = str(caption_text or "").strip()
    if re.match(r"^\s*Table\s+A\d+\.\s*", raw_caption):
        return raw_caption
    raw = re.sub(r"^\s*Table\s+\d+\.\s*", "", raw_caption).strip()
    return f"Table {table_number}. {raw}" if raw else f"Table {table_number}."


def _to_int(value: object) -> int:
    return int(value or 0)


def _to_optional_pct(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round((numerator / denominator) * 100.0, 2)


def _table_exists(db: Session, table_name: str) -> bool:
    return bool(
        db.execute(
            text("SELECT to_regclass(:table_name) IS NOT NULL"),
            {"table_name": f"public.{table_name}"},
        ).scalar()
    )


def surveillance_kpis(weeks: int = 12, db: Session = Depends(get_db)) -> dict:
    """Return programme surveillance KPIs for cases in the recent reporting window."""
    weeks = max(1, min(int(weeks or 12), 104))
    has_sequences = _table_exists(db, "consensus_sequences")
    has_qc = _table_exists(db, "sample_qc_metrics")

    sequence_join = (
        "LEFT JOIN consensus_sequences cs ON cs.sample_id = c.pseudonymised_case_id"
        if has_sequences
        else ""
    )
    qc_join = (
        "LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id = c.pseudonymised_case_id"
        if has_qc
        else ""
    )
    sequenced_expr = "COUNT(DISTINCT cs.sample_id)::int" if has_sequences else "0::int"
    qc_reported_expr = "COUNT(DISTINCT sqm.sample_id)::int" if has_qc else "0::int"
    qc_pass_expr = (
        "COUNT(DISTINCT sqm.sample_id) FILTER (WHERE LOWER(COALESCE(sqm.qc_status, '')) IN ('pass', 'passed'))::int"
        if has_qc
        else "0::int"
    )
    qc_fail_expr = (
        "COUNT(DISTINCT sqm.sample_id) FILTER (WHERE sqm.qc_status IS NOT NULL AND LOWER(COALESCE(sqm.qc_status, '')) NOT IN ('pass', 'passed'))::int"
        if has_qc
        else "0::int"
    )
    contamination_expr = (
        "COUNT(DISTINCT sqm.sample_id) FILTER (WHERE COALESCE(sqm.contamination_flag, false))::int"
        if has_qc
        else "0::int"
    )
    median_expr = (
        "CAST(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (sqm.reported_at::timestamp - c.specimen_date::timestamp)) / 86400.0) "
        "FILTER (WHERE sqm.reported_at IS NOT NULL AND c.specimen_date IS NOT NULL) AS float)"
        if has_qc
        else "NULL::float"
    )

    params = {"weeks": weeks}
    kpi_rows = db.execute(
        text(
            f"""
            SELECT
                COUNT(DISTINCT c.pseudonymised_case_id)::int AS eligible_cases,
                {sequenced_expr} AS sequenced_cases,
                {qc_reported_expr} AS qc_reported_cases,
                {qc_pass_expr} AS qc_pass_cases,
                {qc_fail_expr} AS qc_fail_cases,
                {contamination_expr} AS contamination_flag_cases,
                {median_expr} AS median_days_specimen_to_qc
            FROM cases c
            {sequence_join}
            {qc_join}
            WHERE c.specimen_date >= CURRENT_DATE - (:weeks * INTERVAL '7 days')
            """
        ),
        params,
    ).mappings().first() or {}

    region_rows = db.execute(
        text(
            f"""
            SELECT
                COALESCE(c.geographic_region, 'Unknown') AS region,
                COUNT(DISTINCT c.pseudonymised_case_id)::int AS eligible_cases,
                {sequenced_expr} AS sequenced_cases
            FROM cases c
            {sequence_join}
            WHERE c.specimen_date >= CURRENT_DATE - (:weeks * INTERVAL '7 days')
            GROUP BY COALESCE(c.geographic_region, 'Unknown')
            ORDER BY eligible_cases DESC, region
            """
        ),
        params,
    ).mappings().all()

    eligible_cases = _to_int(kpi_rows["eligible_cases"])
    sequenced_cases = _to_int(kpi_rows["sequenced_cases"])
    qc_reported_cases = _to_int(kpi_rows["qc_reported_cases"])
    qc_pass_cases = _to_int(kpi_rows["qc_pass_cases"])
    median_days = kpi_rows["median_days_specimen_to_qc"]
    if median_days is not None:
        median_days = round(float(median_days), 2)

    return {
        "window_weeks": weeks,
        "eligible_cases": eligible_cases,
        "sequenced_cases": sequenced_cases,
        "sequenced_pct": _to_optional_pct(sequenced_cases, eligible_cases),
        "qc_reported_cases": qc_reported_cases,
        "qc_pass_cases": qc_pass_cases,
        "qc_fail_cases": _to_int(kpi_rows["qc_fail_cases"]),
        "qc_pass_pct": _to_optional_pct(qc_pass_cases, qc_reported_cases),
        "contamination_flag_cases": _to_int(kpi_rows["contamination_flag_cases"]),
        "median_days_specimen_to_qc": median_days,
        "representativeness_by_region": [
            {
                "region": row["region"],
                "eligible_cases": _to_int(row["eligible_cases"]),
                "sequenced_cases": _to_int(row["sequenced_cases"]),
                "sequenced_pct": _to_optional_pct(_to_int(row["sequenced_cases"]), _to_int(row["eligible_cases"])),
            }
            for row in region_rows
        ],
    }


@router.get("/")
def list_cases(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return db.query(Case).offset(offset).limit(limit).all()


@router.get("/kpis")
def case_kpis(weeks: int = Query(12, ge=1, le=104), db: Session = Depends(get_db)):
    return surveillance_kpis(weeks=weeks, db=db)


@router.get("/regions")
def list_regions(db: Session = Depends(get_db)):
    """Return distinct geographic regions present in the cases table."""
    rows = db.execute(
        text(
            "SELECT DISTINCT geographic_region FROM cases "
            "WHERE geographic_region IS NOT NULL "
            "ORDER BY geographic_region"
        )
    ).scalars().all()
    return {"regions": list(rows)}


@router.get("/outbreaker-status")
def outbreaker_status():
    summary_path = _export_path("outbreaker_summary.json")
    provenance = None
    if os.path.exists(summary_path):
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                summary = json.load(f)
                provenance = (summary or {}).get("data_provenance")
        except Exception:
            provenance = None

    return {
        "cases_export": os.path.exists(_export_path("cases.csv")),
        "dna_export": os.path.exists(_export_path("dna.fasta")),
        "results_rds": os.path.exists(_export_path("outbreaker2_results.rds")),
        "provenance": provenance,
        "is_mock": provenance == "mock",
    }


@router.get("/audit-trail")
def audit_trail(limit: int = 50, db: Session = Depends(get_db)):
    """Return recent audit log entries for governance and compliance."""
    rows = db.execute(
        text(
            """
            SELECT audit_id, action, user_id, details, timestamp
            FROM audit_log
            ORDER BY timestamp DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings().all()
    
    return {
        "total_entries": len(rows),
        "entries": [
            {
                "id": row["audit_id"],
                "action": row["action"],
                "user": row["user_id"],
                "timestamp": str(row["timestamp"]),
                "details": row["details"],
            }
            for row in rows
        ],
    }


@router.get("/outbreaker-analysis")
def outbreaker_analysis():
    """Return outbreaker2 analysis results including graphics and summary."""
    result = {
        "status": "no_results",
        "message": "Analysis has not been run yet",
        "graphics": [],
        "summary": None,
        "transmission_network": None,
        "secondary_validation": None,
        "provenance": None,
        "is_mock": None,
    }
    
    # Check for analysis summary
    summary_path = _export_path("outbreaker_summary.json")
    if os.path.exists(summary_path):
        try:
            with open(summary_path, "r") as f:
                result["summary"] = json.load(f)
                result["status"] = "completed"
                summary_provenance = result["summary"].get("data_provenance") if isinstance(result["summary"], dict) else None
                if summary_provenance in {"real", "mock"}:
                    result["provenance"] = summary_provenance
                    result["is_mock"] = summary_provenance == "mock"
        except Exception as e:
            result["status"] = "error"
            result["message"] = str(e)

    network_path = _export_path("transmission_network.json")
    if os.path.exists(network_path):
        try:
            with open(network_path, "r", encoding="utf-8") as f:
                result["transmission_network"] = json.load(f)
                if result["provenance"] is None and isinstance(result["transmission_network"], dict):
                    network_provenance = result["transmission_network"].get("provenance")
                    if network_provenance in {"real", "mock"}:
                        result["provenance"] = network_provenance
                        result["is_mock"] = network_provenance == "mock"
        except Exception:
            result["transmission_network"] = None

    secondary_path = _export_path("secondary_engine_validation.json")
    if os.path.exists(secondary_path):
        try:
            with open(secondary_path, "r", encoding="utf-8") as f:
                result["secondary_validation"] = json.load(f)
        except Exception:
            result["secondary_validation"] = None

    if result["provenance"] is None:
        result["provenance"] = "unknown"
        result["is_mock"] = None
    
    # List available graphics
    graphics_dir = "exports"
    if os.path.exists(graphics_dir):
        # Collect all graphics and sort for consistent order
        graphics_files = [f for f in os.listdir(graphics_dir) 
                         if f.startswith("outbreaker_") and f.endswith(".png")]
        # Sort with preferred order: trace, hist, tree
        order = {"outbreaker_trace.png": 0, "outbreaker_hist.png": 1, "outbreaker_tree.png": 2}
        graphics_files.sort(key=lambda f: order.get(f, 999))
        
        for file in graphics_files:
            result["graphics"].append({
                "name": file,
                "url": f"/cases/outbreaker-image/{file}",
                "type": file.replace("outbreaker_", "").replace(".png", ""),
            })
    
    return result


@router.get("/lineage-dr-validation")
def lineage_dr_validation(db: Session = Depends(get_db)):
    """Return lineage/drug-resistance integration validation artifact."""
    analysis_summary = _lineage_analysis_summary(db)
    analysis_epi_summary = _lineage_epi_summary(db)

    path = _export_path("lineage_dr_validation.json")
    if not os.path.exists(path):
        return {
            "status": "no_results",
            "message": "Lineage/DR validation has not been run yet",
            "artifact_path": path,
            "analysis_summary": analysis_summary,
            "analysis_epi_summary": analysis_epi_summary,
        }

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
            "artifact_path": path,
            "analysis_summary": analysis_summary,
            "analysis_epi_summary": analysis_epi_summary,
        }

    payload["artifact_path"] = path
    payload["analysis_summary"] = analysis_summary
    payload["analysis_epi_summary"] = analysis_epi_summary
    payload["effective_engines"] = _derive_effective_engine_status(payload)
    return payload


def _safe_html(value) -> str:
    """Escape a value for safe insertion into the static HTML report."""
    return html_lib.escape("" if value is None else str(value), quote=True)


def _load_export_json(filename: str):
    path = _export_path(filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        return {"error": f"Could not read {filename}: {exc}"}


def _load_export_csv(filename: str, limit: int | None = 50) -> list[dict]:
    path = _export_path(filename)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if limit is None:
                return list(reader)
            return [row for _, row in zip(range(limit), reader)]
    except Exception:
        return []


def _html_kv_table(mapping: dict | None, fields: list[tuple[str, str]]) -> str:
    if not mapping:
        return '<p class="muted">No data available.</p>'
    rows = []
    for label, key in fields:
        value = mapping.get(key)
        if isinstance(value, float):
            value = f"{value:.2f}"
        rows.append(f"<tr><th>{_safe_html(label)}</th><td>{_safe_html(value if value is not None else 'Not available')}</td></tr>")
    return '<table class="kv"><tbody>' + ''.join(rows) + '</tbody></table>'


def _html_data_table(rows: list[dict], columns: list[tuple[str, str]], empty_message: str) -> str:
    if not rows:
        return f'<p class="muted">{_safe_html(empty_message)}</p>'
    header = ''.join(f'<th>{_safe_html(label)}</th>' for label, _ in columns)
    body = []
    for row in rows:
        body.append('<tr>' + ''.join(f'<td>{_safe_html(row.get(key, ""))}</td>' for _, key in columns) + '</tr>')
    return '<table><thead><tr>' + header + '</tr></thead><tbody>' + ''.join(body) + '</tbody></table>'


def _image_data_uri(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return None


def _build_outbreak_report_html(db: Session, full: bool = False) -> str:
    """Build a static HTML outbreak report from database counts and export artifacts."""
    os.makedirs(_export_path(), exist_ok=True)

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    total_cases = db.execute(text("SELECT COUNT(*) FROM cases")).scalar() or 0
    clustered_cases = db.execute(text("SELECT COUNT(DISTINCT sample_id) FROM case_clusters")).scalar() or 0
    open_clusters = db.execute(
        text("SELECT COUNT(*) FROM clusters WHERE investigation_status = 'open'")
    ).scalar() or 0

    summary_data = _load_export_json("outbreaker_summary.json")
    transmission_data = _load_export_json("transmission_network.json")
    lineage_dr_data = _load_export_json("lineage_dr_validation.json")
    secondary_validation_data = _load_export_json("secondary_engine_validation.json")
    method_comparison_data = _load_export_json("cluster_method_comparison.json")
    sequence_summary_data = _load_export_json("sequence_clustering_summary.json")

    try:
        kpi_data = surveillance_kpis(weeks=12, db=db)
    except Exception as exc:
        kpi_data = {"warning": str(exc)}

    row_limit = None if full else 25
    network_limit = None if full else 15
    action_rows = _load_export_csv("appendix_a_case_level_actions.csv", limit=row_limit)
    discordance_rows = _load_export_csv("appendix_b_full_discordance_review.csv", limit=row_limit)

    key_nodes = []
    if isinstance(transmission_data, dict):
        key_nodes = transmission_data.get("key_nodes") or []
    edges = []
    if isinstance(transmission_data, dict):
        edges = transmission_data.get("edges") or transmission_data.get("transmission_edges") or []

    graphics_html = []
    for image_path in sorted(Path(_export_path()).glob("outbreaker_*.png")):
        uri = _image_data_uri(str(image_path))
        if uri:
            label = image_path.stem.replace("outbreaker_", "").replace("_", " ").title()
            graphics_html.append(
                f'<figure><img src="{uri}" alt="{_safe_html(label)}"><figcaption>{_safe_html(label)}</figcaption></figure>'
            )

    report_label = "Full HTML" if full else "Short HTML"
    report_filename = (
        "outbreaker_investigation_report_full.html"
        if full
        else "outbreaker_investigation_report.html"
    )
    action_heading = "Case-level operational actions (all rows)" if full else "Case-level operational actions (first 25 rows)"
    discordance_heading = "Discordance review (all rows)" if full else "Discordance review (first 25 rows)"
    priority_heading = "Priority nodes (all rows)" if full else "Priority nodes (first 15 rows)"
    links_heading = "Transmission links (all rows)" if full else "Top transmission links (first 15 rows)"
    raw_artifact_section = ""
    if full:
        raw_artifact_section = f"""
  <section class="card" id="raw-artifacts">
    <h2>Full machine-readable artifacts</h2>
    <p class="muted">These sections mirror the JSON exports used to produce the report so reviewers can inspect the complete source artifacts alongside the summary tables.</p>
    <h3>Outbreaker2 summary artifact</h3>
    <pre>{_safe_html(json.dumps(summary_data, indent=2, default=str) if summary_data else 'No outbreaker summary artifact found.')}</pre>
    <h3>Transmission network artifact</h3>
    <pre>{_safe_html(json.dumps(transmission_data, indent=2, default=str) if transmission_data else 'No transmission network artifact found.')}</pre>
    <h3>Sequence clustering summary artifact</h3>
    <pre>{_safe_html(json.dumps(sequence_summary_data, indent=2, default=str) if sequence_summary_data else 'No sequence clustering summary artifact found.')}</pre>
  </section>"""

    publication_notes = [
        f"This is the {report_label.lower()} version of the browser report; the full and short versions are saved side by side in exports/ for review.",
        "Single-file HTML output is easier to publish online and review in browsers than a paginated PDF.",
        "Responsive tables and figures reduce the PDF wrapping and page-break formatting issues previously seen.",
        "Publish only after local information-governance review; this file may contain case-level operational details.",
    ]

    css = """
    :root{--navy:#16324f;--steel:#365f7f;--muted:#64748b;--line:#d8e0ea;--bg:#f6f8fb;--card:#fff;--accent:#0f766e;}
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:#172033;font-family:Arial,Helvetica,sans-serif;line-height:1.45}
    header{background:linear-gradient(135deg,var(--navy),#254f78);color:white;padding:2.2rem 6vw} header p{max-width:70rem;margin:.3rem 0 0;color:#e6eef7}
    main{max-width:1180px;margin:0 auto;padding:1.5rem 1rem 3rem}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:0 8px 22px rgba(21,38,64,.07);margin:1rem 0;padding:1.2rem}
    h1{font-size:2rem;margin:0 0 .2rem} h2{color:var(--navy);font-size:1.25rem;margin:.2rem 0 .8rem;border-bottom:2px solid var(--line);padding-bottom:.35rem} h3{color:var(--steel);font-size:1rem;margin:1rem 0 .5rem}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.8rem}.metric{border-left:5px solid var(--accent);background:#f8fffd;border-radius:10px;padding:.85rem}.metric strong{display:block;font-size:1.5rem;color:var(--navy)}
    table{width:100%;border-collapse:collapse;margin:.7rem 0;display:block;overflow-x:auto} th,td{border:1px solid var(--line);padding:.5rem;text-align:left;vertical-align:top} th{background:#eaf1f8;color:#17324f}.kv{display:table}.kv th{width:32%}
    .muted{color:var(--muted)} .note{background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:.75rem}.toc a{display:inline-block;margin:.2rem .7rem .2rem 0;color:#0f4c81}.figures{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1rem}.figures img{width:100%;height:auto;border:1px solid var(--line);border-radius:8px;background:white}.figures figcaption{text-align:center;color:var(--muted);font-size:.9rem;margin-top:.3rem}
    pre{white-space:pre-wrap;background:#0f172a;color:#e2e8f0;border-radius:10px;padding:1rem;overflow:auto}.footer{font-size:.88rem;color:var(--muted)}
    @media print{body{background:white}.card{box-shadow:none;break-inside:avoid} header{background:white;color:#172033;border-bottom:2px solid var(--line)} header p{color:#334155}}
    """

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Outbreak Investigation Report ({_safe_html(report_label)})</title>
  <style>{css}</style>
</head>
<body>
<header>
  <h1>NI TB Genomic Surveillance</h1>
  <p>Outbreak Investigation Report — generated {_safe_html(generated_at)}</p>
</header>
<main>
  <section class="card note">
    <strong>HTML publication note:</strong>
    <ul>{''.join(f'<li>{_safe_html(note)}</li>' for note in publication_notes)}</ul>
  </section>
  <nav class="card toc" aria-label="Report sections">
    <a href="#summary">Executive summary</a><a href="#outbreaker">Outbreaker2</a><a href="#transmission">Transmission network</a><a href="#lineage">Lineage/DR</a><a href="#quality">Quality and methods</a><a href="#actions">Action appendices</a>{'<a href="#raw-artifacts">Raw artifacts</a>' if full else ''}<a href="#figures">Figures</a>
  </nav>
  <section class="card" id="summary">
    <h2>Executive summary</h2>
    <div class="grid">
      <div class="metric"><span>Total cases</span><strong>{_safe_html(total_cases)}</strong></div>
      <div class="metric"><span>Clustered cases</span><strong>{_safe_html(clustered_cases)}</strong></div>
      <div class="metric"><span>Open clusters</span><strong>{_safe_html(open_clusters)}</strong></div>
      <div class="metric"><span>Report format</span><strong>{_safe_html(report_label)}</strong></div>
    </div>
    <h3>Surveillance KPIs</h3>
    {_html_kv_table(kpi_data if isinstance(kpi_data, dict) else None, [('Eligible cases','eligible_cases'),('Sequenced cases','sequenced_cases'),('Sequencing coverage %','sequencing_coverage_pct'),('QC pass %','qc_pass_pct'),('Warning','warning')])}
  </section>
  <section class="card" id="outbreaker">
    <h2>Outbreaker2 summary</h2>
    {_html_kv_table(summary_data if isinstance(summary_data, dict) else None, [('Analysis engine','analysis_engine'),('Generated at','generated_at'),('Case count','case_count'),('Posterior samples','posterior_samples'),('Likelihood mean','likelihood_mean'),('Likelihood SD','likelihood_sd')])}
  </section>
  <section class="card" id="transmission">
    <h2>Transmission network</h2>
    {_html_kv_table(transmission_data if isinstance(transmission_data, dict) else None, [('Generated at','generated_at'),('Inference source','inference_source'),('Provenance','provenance'),('Node count','node_count'),('Edge count','edge_count'),('High-confidence edges','high_confidence_edges')])}
    <h3>{_safe_html(priority_heading)}</h3>
    {_html_data_table(key_nodes if network_limit is None else key_nodes[:network_limit], [('Case','case_id'),('Cluster','cluster_id'),('Region','region'),('Risk score','risk_score'),('Risk band','risk_band'),('Outgoing','outgoing_links'),('Incoming','incoming_links')], 'No priority-node data available.')}
    <h3>{_safe_html(links_heading)}</h3>
    {_html_data_table(edges if network_limit is None else edges[:network_limit], [('From','source'),('To','target'),('Probability','probability'),('Confidence','confidence'),('Inference','inference')], 'No transmission-link data available.')}
  </section>
  <section class="card" id="lineage">
    <h2>Lineage and drug-resistance readiness</h2>
    {_html_kv_table(lineage_dr_data if isinstance(lineage_dr_data, dict) else None, [('Generated at','generated_at'),('Status','status'),('Scaffold version','scaffold_version')])}
    <h3>Raw validation artifact</h3>
    <pre>{_safe_html(json.dumps(lineage_dr_data, indent=2, default=str) if lineage_dr_data else 'No lineage/DR validation artifact found.')}</pre>
  </section>
  <section class="card" id="quality">
    <h2>Quality, validation, and method comparison</h2>
    <h3>Secondary engine validation</h3>
    <pre>{_safe_html(json.dumps(secondary_validation_data, indent=2, default=str) if secondary_validation_data else 'No secondary engine validation artifact found.')}</pre>
    <h3>Cross-method clustering comparison</h3>
    <pre>{_safe_html(json.dumps(method_comparison_data, indent=2, default=str) if method_comparison_data else 'No method comparison artifact found.')}</pre>
    <h3>Sequence clustering snapshot</h3>
    {_html_kv_table(sequence_summary_data if isinstance(sequence_summary_data, dict) else None, [('Status','status'),('Method','method'),('Threshold SNP distance','threshold_snp_distance'),('Sequenced cases','sequenced_cases'),('Clustered cases','clustered_cases'),('Cluster count','cluster_count')])}
  </section>
  <section class="card" id="actions">
    <h2>Action appendices</h2>
    <h3>{_safe_html(action_heading)}</h3>
    {_html_data_table(action_rows, [('Case','Case'),('Cluster','Cluster'),('Pairwise SNP?','Pairwise SNP?'),('Posterior','Posterior'),('Tier','Tier'),('QC','QC'),('Recommended action','Recommended action')], 'No case-level action export found.')}
    <h3>{_safe_html(discordance_heading)}</h3>
    {_html_data_table(discordance_rows, [('Case pair','Case Pair'),('Pairwise SNP result','Pairwise SNP result'),('Outbreaker2','Outbreaker2'),('Pairwise SNP','Pairwise SNP'),('Posterior','Posterior'),('Code','Code'),('Interpretation','Interpretation')], 'No discordance-review export found.')}
  </section>
  {raw_artifact_section}
  <section class="card" id="figures">
    <h2>Figures</h2>
    <div class="figures">{''.join(graphics_html) if graphics_html else '<p class="muted">No outbreak graphics found in exports/.</p>'}</div>
  </section>
  <section class="card footer">
    <p>Generated from TB Genomics backend artifacts. For operational use, interpret genomic findings with clinical history, contact tracing, epidemiology, QC status, and local governance review.</p>
  </section>
</main>
</body>
</html>"""

    output_path = _export_path(report_filename)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return html


@router.get("/outbreak-report.html", response_class=HTMLResponse)
def outbreak_report_html(db: Session = Depends(get_db)):
    """Generate and return the short publication-friendly static HTML outbreak report."""
    enforce_operational_dataset(db, "cases/outbreak-report.html")
    return HTMLResponse(content=_build_outbreak_report_html(db, full=False))


@router.get("/outbreak-report.full.html", response_class=HTMLResponse)
def outbreak_report_full_html(db: Session = Depends(get_db)):
    """Generate and return the full static HTML outbreak report alongside the short version."""
    enforce_operational_dataset(db, "cases/outbreak-report.full.html")
    return HTMLResponse(content=_build_outbreak_report_html(db, full=True))


@router.get("/outbreak-report")
def outbreak_report(db: Session = Depends(get_db)):
    """Generate and return a PDF outbreak investigation report."""
    enforce_operational_dataset(db, "cases/outbreak-report")

    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import BaseDocTemplate, CondPageBreak, Frame, Image, KeepTogether, NextPageTemplate, PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.utils import ImageReader
    except Exception as e:
        return {"error": f"PDF generation dependency missing: {e}"}

    os.makedirs(_export_path(), exist_ok=True)
    report_path = _export_path("outbreaker_investigation_report.pdf")

    # Guard: if the PDF is already open (e.g. in a viewer), fail fast with a
    # clear 409 rather than a cryptic PermissionError deep inside ReportLab.
    if os.path.exists(report_path):
        try:
            with open(report_path, "a+b"):
                pass
        except PermissionError:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=409,
                content={
                    "detail": (
                        "Cannot generate report: "
                        f"'{os.path.basename(report_path)}' is open in another application. "
                        "Close the PDF viewer and try again."
                    )
                },
            )

    total_cases = db.execute(text("SELECT COUNT(*) FROM cases")).scalar() or 0
    clustered_cases = db.execute(text("SELECT COUNT(DISTINCT sample_id) FROM case_clusters")).scalar() or 0
    open_clusters = db.execute(
        text("SELECT COUNT(*) FROM clusters WHERE investigation_status = 'open'")
    ).scalar() or 0

    summary_data = None
    summary_path = _export_path("outbreaker_summary.json")
    if os.path.exists(summary_path):
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                summary_data = json.load(f)
        except Exception:
            summary_data = None

    transmission_data = None
    transmission_path = _export_path("transmission_network.json")
    if os.path.exists(transmission_path):
        try:
            with open(transmission_path, "r", encoding="utf-8") as f:
                transmission_data = json.load(f)
        except Exception:
            transmission_data = None

    def load_json_artifact(filename: str):
        path = _export_path(filename)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    lineage_dr_data = load_json_artifact("lineage_dr_validation.json")
    secondary_validation_data = load_json_artifact("secondary_engine_validation.json")
    method_comparison_data = load_json_artifact("cluster_method_comparison.json")
    sequence_summary_data = load_json_artifact("sequence_clustering_summary.json")

    kpi_data = None
    try:
        kpi_data = surveillance_kpis(weeks=12, db=db)
    except Exception:
        kpi_data = None

    qc_table_exists = db.execute(
        text("SELECT to_regclass('public.sample_qc_metrics') IS NOT NULL")
    ).scalar()

    weekly_trends = []
    if qc_table_exists:
        weekly_trends = db.execute(
            text(
                """
                WITH week_windows AS (
                    SELECT (DATE_TRUNC('week', CURRENT_DATE) - (s * INTERVAL '7 days'))::date AS week_start
                    FROM generate_series(11, 0, -1) s
                ),
                weekly AS (
                    SELECT
                        ww.week_start,
                        COUNT(c.pseudonymised_case_id)::int AS eligible_cases,
                        COUNT(cs.sample_id)::int AS sequenced_cases,
                        COUNT(sqm.sample_id)::int AS qc_reported_cases,
                        COUNT(*) FILTER (
                            WHERE LOWER(COALESCE(sqm.qc_status, '')) IN ('pass', 'passed')
                        )::int AS qc_pass_cases
                    FROM week_windows ww
                    LEFT JOIN cases c
                        ON c.specimen_date >= ww.week_start
                        AND c.specimen_date < ww.week_start + INTERVAL '7 days'
                    LEFT JOIN consensus_sequences cs ON cs.sample_id = c.pseudonymised_case_id
                    LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id = c.pseudonymised_case_id
                    GROUP BY ww.week_start
                )
                SELECT
                    week_start,
                    eligible_cases,
                    sequenced_cases,
                    CASE
                        WHEN eligible_cases = 0 THEN NULL
                        ELSE ROUND((sequenced_cases::numeric / eligible_cases::numeric) * 100.0, 2)
                    END AS sequenced_pct,
                    CASE
                        WHEN qc_reported_cases = 0 THEN NULL
                        ELSE ROUND((qc_pass_cases::numeric / qc_reported_cases::numeric) * 100.0, 2)
                    END AS qc_pass_pct
                FROM weekly
                ORDER BY week_start
                """
            )
        ).mappings().all()
    else:
        weekly_trends = db.execute(
            text(
                """
                WITH week_windows AS (
                    SELECT (DATE_TRUNC('week', CURRENT_DATE) - (s * INTERVAL '7 days'))::date AS week_start
                    FROM generate_series(11, 0, -1) s
                ),
                weekly AS (
                    SELECT
                        ww.week_start,
                        COUNT(c.pseudonymised_case_id)::int AS eligible_cases,
                        COUNT(cs.sample_id)::int AS sequenced_cases
                    FROM week_windows ww
                    LEFT JOIN cases c
                        ON c.specimen_date >= ww.week_start
                        AND c.specimen_date < ww.week_start + INTERVAL '7 days'
                    LEFT JOIN consensus_sequences cs ON cs.sample_id = c.pseudonymised_case_id
                    GROUP BY ww.week_start
                )
                SELECT
                    week_start,
                    eligible_cases,
                    sequenced_cases,
                    CASE
                        WHEN eligible_cases = 0 THEN NULL
                        ELSE ROUND((sequenced_cases::numeric / eligible_cases::numeric) * 100.0, 2)
                    END AS sequenced_pct,
                    NULL::numeric AS qc_pass_pct
                FROM weekly
                ORDER BY week_start
                """
            )
        ).mappings().all()

    cluster_action_rows = db.execute(
        text(
            """
            WITH cluster_stats AS (
                SELECT
                    cc.cluster_id,
                    COUNT(*)::int AS case_count,
                    MAX(c.specimen_date) AS most_recent_specimen,
                    COUNT(DISTINCT c.geographic_region)::int AS region_count,
                    COALESCE(cl.investigation_status, 'unknown') AS investigation_status,
                    EXTRACT(DAY FROM (CURRENT_DATE::timestamp - MAX(c.specimen_date)::timestamp))::int AS recency_days
                FROM case_clusters cc
                JOIN cases c ON c.pseudonymised_case_id = cc.sample_id
                LEFT JOIN clusters cl ON cl.cluster_id = cc.cluster_id
                GROUP BY cc.cluster_id, cl.investigation_status
            )
            SELECT
                cluster_id,
                case_count,
                region_count,
                most_recent_specimen,
                recency_days,
                investigation_status,
                (
                    (case_count * 2)
                    + (region_count * 3)
                    + CASE
                        WHEN recency_days <= 14 THEN 3
                        WHEN recency_days <= 30 THEN 2
                        WHEN recency_days <= 60 THEN 1
                        ELSE 0
                    END
                    + CASE WHEN investigation_status = 'open' THEN 3 ELSE 0 END
                )::int AS priority_score
            FROM cluster_stats
            ORDER BY priority_score DESC, case_count DESC, most_recent_specimen DESC
            LIMIT 10
            """
        )
    ).mappings().all()

    case_rows = db.execute(
        text(
            """
            SELECT
                c.pseudonymised_case_id::text AS case_id,
                c.specimen_date,
                c.geographic_region,
                c.case_status,
                cc.cluster_id::text AS cluster_id,
                cl.snp_distance,
                cl.investigation_status,
                ti.lineage,
                ti.predicted_drug_resistance,
                ti.resistance_mutations,
                ti.interpretation_summary,
                cs.sequence,
                sqm.qc_status,
                sqm.coverage_breadth,
                sqm.mean_depth,
                sqm.contamination_flag,
                sqm.ambiguous_base_percent
            FROM cases c
            LEFT JOIN case_clusters cc ON cc.sample_id = c.pseudonymised_case_id
            LEFT JOIN clusters cl ON cl.cluster_id = cc.cluster_id
            LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
            LEFT JOIN consensus_sequences cs ON cs.sample_id = c.pseudonymised_case_id
            LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id = c.pseudonymised_case_id
            ORDER BY c.specimen_date DESC NULLS LAST, c.pseudonymised_case_id
            """
        )
    ).mappings().all()

    case_by_id = {str(row.get("case_id")): row for row in case_rows if row.get("case_id")}
    sequence_by_case = {
        str(row.get("case_id")): str(row.get("sequence") or "").strip().upper()
        for row in case_rows
        if row.get("case_id") and row.get("sequence")
    }
    pairwise_snp_matrix = _pairwise_matrix(sequence_by_case)
    transmission_edges = (transmission_data or {}).get("edges") or []

    best_incoming = {}
    best_outgoing = {}
    outbreaker_pair_prob = {}

    for edge in transmission_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if not source or not target:
            continue
        prob = float(edge.get("probability") or 0.0)

        if target not in best_incoming or prob > best_incoming[target]["probability"]:
            best_incoming[target] = {"source": source, "probability": prob}
        if source not in best_outgoing or prob > best_outgoing[source]["probability"]:
            best_outgoing[source] = {"target": target, "probability": prob}

        pair_key = tuple(sorted([source, target]))
        if pair_key not in outbreaker_pair_prob or prob > outbreaker_pair_prob[pair_key]:
            outbreaker_pair_prob[pair_key] = prob

    sequence_cluster_members = {}
    for row in case_rows:
        cid = str(row.get("cluster_id") or "")
        case_id = str(row.get("case_id") or "")
        if cid and case_id:
            sequence_cluster_members.setdefault(cid, []).append(case_id)

    pairwise_links_le_5 = 0
    pairwise_links_le_12 = 0
    sequence_pair_set = set()
    nearest_neighbor_snp = {}
    nearest_neighbor_partner = {}
    for pair, dist in pairwise_snp_matrix.items():
        if dist <= 5:
            pairwise_links_le_5 += 1
        if dist <= 12:
            pairwise_links_le_12 += 1
            sequence_pair_set.add(pair)

    for case_id, seq in sequence_by_case.items():
        relevant = []
        for other_case_id, other_seq in sequence_by_case.items():
            if other_case_id == case_id:
                continue
            dist = pairwise_snp_matrix.get(_pair_key(case_id, other_case_id))
            if dist is not None:
                relevant.append((dist, other_case_id))
        if relevant:
            best_dist, best_partner = sorted(relevant, key=lambda item: (item[0], item[1]))[0]
            nearest_neighbor_snp[case_id] = int(best_dist)
            nearest_neighbor_partner[case_id] = best_partner

    cluster_pairwise_stats = {}
    for cluster_id, members in sequence_cluster_members.items():
        unique_members = sorted(set(members))
        sequenced_members = [case_id for case_id in unique_members if sequence_by_case.get(case_id)]
        distances = [pairwise_snp_matrix[_pair_key(left, right)] for left, right in combinations(sequenced_members, 2) if _pair_key(left, right) in pairwise_snp_matrix]
        cluster_pairwise_stats[cluster_id] = {
            "member_count": len(unique_members),
            "sequenced_member_count": len(sequenced_members),
            "pairwise_min": min(distances) if distances else None,
            "pairwise_median": median(distances) if distances else None,
            "pairwise_max": max(distances) if distances else None,
            "links_le_5": sum(1 for value in distances if value <= 5),
            "links_le_12": sum(1 for value in distances if value <= 12),
            "nearest_neighbor_min": min((nearest_neighbor_snp.get(case_id) for case_id in sequenced_members if nearest_neighbor_snp.get(case_id) is not None), default=None),
        }

    high_confidence_edges = [
        e for e in transmission_edges if float(e.get("probability") or 0.0) >= 0.70
    ]
    high_confidence_all_count = len(high_confidence_edges)
    high_confidence_snapshot_count = int(
        (transmission_data.get("high_confidence_edges", 0) if transmission_data else 0) or 0
    )

    qc_detail_rows = [
        row for row in case_rows
        if str(row.get("qc_status") or "").lower() not in ("pass", "passed")
        or bool(row.get("contamination_flag"))
    ]

    if not qc_detail_rows:
        qc_detail_rows = [r for r in case_rows if r.get("qc_status")][:15]

    qc_status_counts = {
        "pass": 0,
        "fail": 0,
        "not_reported": 0,
        "contamination": 0,
    }
    for row in case_rows:
        status = str(row.get("qc_status") or "not_reported").lower()
        contamination = bool(row.get("contamination_flag"))
        if contamination:
            qc_status_counts["contamination"] += 1
        if status in ("pass", "passed"):
            qc_status_counts["pass"] += 1
        elif status in ("", "not_reported", "na", "n/a", "unknown"):
            qc_status_counts["not_reported"] += 1
        else:
            qc_status_counts["fail"] += 1

    excluded_from_outbreaker = sum(
        1
        for row in case_rows
        if str(row.get("qc_status") or "").lower() not in ("pass", "passed") or bool(row.get("contamination_flag"))
    )

    mutation_rows_raw = db.execute(
        text(
            """
            SELECT
                sample_id::text AS case_id,
                resistance_mutations,
                predicted_drug_resistance
            FROM tb_interpretation
            WHERE resistance_mutations IS NOT NULL
            AND resistance_mutations::text NOT IN ('null', '{}', '[]')
            ORDER BY sample_id
            LIMIT 80
            """
        )
    ).mappings().all()

    mutation_validation_rows = []
    for row in mutation_rows_raw:
        predicted_text = _resistance_profile_text(row.get("predicted_drug_resistance"))
        for mut in _iter_resistance_mutations(row.get("resistance_mutations")):
            gene = str(mut.get("gene") or "n/a")
            drug = str(mut.get("drug") or "n/a")
            validation = _drug_gene_compatibility(drug, gene)
            mutation_validation_rows.append(
                {
                    "case_id": str(row.get("case_id") or ""),
                    "drug": drug,
                    "mutation": str(mut.get("mutation") or "n/a"),
                    "gene": gene,
                    "confidence": str(mut.get("confidence") or "n/a"),
                    "validation": validation,
                    "expected_genes": ", ".join(_expected_genes_for_drug(drug)) or "n/a",
                    "predicted_text": predicted_text,
                    "phenotypic_dst": "pending",
                }
            )

    cluster_epi_rows = db.execute(
        text(
            """
            WITH base AS (
                SELECT
                    cc.cluster_id::text AS cluster_id,
                    c.pseudonymised_case_id::text AS case_id,
                    c.specimen_date,
                    c.geographic_region,
                    COALESCE(cl.investigation_status, 'unknown') AS investigation_status,
                    cl.snp_distance,
                    LOWER(CAST(ti.predicted_drug_resistance AS text)) AS dr_text
                FROM case_clusters cc
                JOIN cases c ON c.pseudonymised_case_id = cc.sample_id
                LEFT JOIN clusters cl ON cl.cluster_id = cc.cluster_id
                LEFT JOIN tb_interpretation ti ON ti.sample_id = cc.sample_id
            ),
            index_case AS (
                SELECT DISTINCT ON (cluster_id)
                    cluster_id,
                    case_id AS suspected_index_case
                FROM base
                ORDER BY cluster_id, specimen_date ASC NULLS LAST, case_id
            )
            SELECT
                b.cluster_id,
                COUNT(*)::int AS cases,
                MIN(b.specimen_date) AS first_specimen,
                MAX(b.specimen_date) AS latest_specimen,
                ROUND(AVG(COALESCE(b.snp_distance, 0))::numeric, 1) AS median_snp_proxy,
                MAX(COALESCE(b.snp_distance, 0))::int AS max_snp_proxy,
                COUNT(*) FILTER (
                    WHERE b.dr_text LIKE '%rifamp%' AND (b.dr_text LIKE '%resistant%' OR b.dr_text LIKE '%\"r\"%')
                )::int AS rr_cases,
                COUNT(*) FILTER (
                    WHERE b.dr_text LIKE '%rifamp%'
                    AND b.dr_text LIKE '%isoniazid%'
                    AND (b.dr_text LIKE '%resistant%' OR b.dr_text LIKE '%\"r\"%')
                )::int AS mdr_cases,
                COUNT(*) FILTER (WHERE b.specimen_date >= CURRENT_DATE - INTERVAL '30 days')::int AS recent_30d,
                COUNT(*) FILTER (WHERE b.specimen_date >= CURRENT_DATE - INTERVAL '60 days')::int AS recent_60d,
                COUNT(*) FILTER (WHERE b.specimen_date >= CURRENT_DATE - INTERVAL '90 days')::int AS recent_90d,
                MAX(b.investigation_status) AS investigation_status,
                i.suspected_index_case
            FROM base b
            LEFT JOIN index_case i ON i.cluster_id = b.cluster_id
            GROUP BY b.cluster_id, i.suspected_index_case
            ORDER BY cases DESC, latest_specimen DESC
            LIMIT 20
            """
        )
    ).mappings().all()

    completeness_row = db.execute(
        text(
            """
            SELECT
                COUNT(*)::int AS total_cases,
                COUNT(*) FILTER (WHERE specimen_date IS NOT NULL)::int AS specimen_date_present,
                COUNT(*) FILTER (WHERE geographic_region IS NOT NULL AND BTRIM(geographic_region) <> '')::int AS region_present,
                COUNT(*) FILTER (WHERE ti.lineage IS NOT NULL AND BTRIM(ti.lineage) <> '')::int AS lineage_present,
                COUNT(*) FILTER (
                    WHERE ti.predicted_drug_resistance IS NOT NULL
                    AND ti.predicted_drug_resistance::text NOT IN ('null', '{}', '[]')
                )::int AS resistance_present,
                COUNT(*) FILTER (WHERE c.local_lab_sample_id IS NOT NULL AND BTRIM(c.local_lab_sample_id) <> '')::int AS notification_proxy_present,
                COUNT(*) FILTER (WHERE sqm.sample_id IS NOT NULL)::int AS qc_present
            FROM cases c
            LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
            LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id = c.pseudonymised_case_id
            """
        )
    ).mappings().first()

    graphic_files = [
        "outbreaker_trace.png",
        "outbreaker_hist.png",
        "outbreaker_tree.png",
        "outbreaker_phylo.png",
        "outbreaker_resistance.png",
    ]
    existing_graphics = [g for g in graphic_files if os.path.exists(_export_path(g))]

    def build_report_image(image_path: str):
        max_width = 6.4 * inch
        max_height = 4.4 * inch
        img_reader = ImageReader(image_path)
        original_width, original_height = img_reader.getSize()

        if not original_width or not original_height:
            return Image(image_path, width=max_width, height=max_height)

        scale = min(max_width / original_width, max_height / original_height)
        scaled_width = original_width * scale
        scaled_height = original_height * scale
        return Image(image_path, width=scaled_width, height=scaled_height)

    def build_trend_chart() -> str | None:
        if not weekly_trends:
            return None
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            labels = [str(row["week_start"])[5:] for row in weekly_trends]
            coverage = [float(row["sequenced_pct"]) if row["sequenced_pct"] is not None else 0.0 for row in weekly_trends]
            qc_pass = [
                float(row["qc_pass_pct"]) if row["qc_pass_pct"] is not None else None
                for row in weekly_trends
            ]

            plt.figure(figsize=(8.2, 2.6))
            plt.plot(labels, coverage, marker="o", linewidth=1.8, label="Sequencing coverage %")
            if any(v is not None for v in qc_pass):
                qc_pass_clean = [v if v is not None else 0.0 for v in qc_pass]
                plt.plot(labels, qc_pass_clean, marker="s", linewidth=1.6, label="QC pass %")

            plt.ylim(0, 100)
            plt.ylabel("Percent")
            plt.xlabel("Week")
            plt.grid(True, linestyle="--", alpha=0.35)
            plt.legend(loc="lower right", fontsize=8)
            plt.tight_layout()

            chart_path = _export_path("outbreaker_weekly_trends.png")
            plt.savefig(chart_path, dpi=140)
            plt.close()
            return chart_path
        except Exception:
            return None

    _report_meta = {
        "status": "DEVELOPMENT / INTERNAL DRAFT ONLY — DO NOT CIRCULATE EXTERNALLY",
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }

    def build_doc(output_file: str):
        def _draw_page(canvas, doc):
            canvas.saveState()
            pw, ph = canvas._pagesize
            lm, rm = doc.leftMargin, doc.rightMargin
            # ── Header ──────────────────────────────────────────────────────────
            canvas.setFont("Helvetica-Bold", 6.5)
            canvas.setFillColor(colors.HexColor("#1d3557"))
            canvas.drawString(lm, ph - 24, "NI TB Genomic Surveillance — Outbreak Investigation Report")
            canvas.setFont("Helvetica", 6.5)
            canvas.setFillColor(colors.HexColor("#457b9d"))
            canvas.drawRightString(pw - rm, ph - 24, f"Generated: {_report_meta['generated_at']}  |  CONFIDENTIAL")
            # Double-rule header separator: thick navy + thin steel
            canvas.setStrokeColor(colors.HexColor("#1d3557"))
            canvas.setLineWidth(1.2)
            canvas.line(lm, ph - 28, pw - rm, ph - 28)
            canvas.setStrokeColor(colors.HexColor("#a8c8e1"))
            canvas.setLineWidth(0.4)
            canvas.line(lm, ph - 30, pw - rm, ph - 30)
            # ── Footer ──────────────────────────────────────────────────────────
            canvas.setStrokeColor(colors.HexColor("#c5d5e4"))
            canvas.setLineWidth(0.4)
            canvas.line(lm, 28, pw - rm, 28)
            if doc.page == 1:
                canvas.setFillColor(colors.HexColor("#e63946"))
                canvas.setFont("Helvetica-Bold", 6.5)
                canvas.drawCentredString(pw / 2, 16, _report_meta["status"])
            else:
                canvas.setFillColor(colors.HexColor("#8a9aaa"))
                canvas.setFont("Helvetica", 5.8)
                canvas.drawCentredString(pw / 2, 14, _report_meta["status"])
            canvas.setFont("Helvetica", 6.5)
            canvas.setFillColor(colors.HexColor("#457b9d"))
            canvas.drawRightString(pw - rm, 16, f"Page {doc.page}")
            canvas.restoreState()

        doc_obj = BaseDocTemplate(
            output_file,
            pagesize=A4,
            rightMargin=32,
            leftMargin=32,
            topMargin=48,
            bottomMargin=40,
        )
        portrait_frame = Frame(
            doc_obj.leftMargin,
            doc_obj.bottomMargin,
            doc_obj.width,
            doc_obj.height,
            id="portrait_frame",
        )
        land_w, land_h = landscape(A4)
        landscape_frame = Frame(
            doc_obj.leftMargin,
            doc_obj.bottomMargin,
            land_w - doc_obj.leftMargin - doc_obj.rightMargin,
            land_h - doc_obj.topMargin - doc_obj.bottomMargin,
            id="landscape_frame",
        )
        doc_obj.addPageTemplates([
            PageTemplate(id="Portrait", frames=[portrait_frame], pagesize=A4, onPage=_draw_page),
            PageTemplate(id="Landscape", frames=[landscape_frame], pagesize=landscape(A4), onPage=_draw_page),
        ])
        return doc_obj

    doc = build_doc(report_path)
    styles = getSampleStyleSheet()

    # ── Brand palette (single source of truth) ────────────────────────────────
    # Navy / primary
    C_NAVY       = colors.HexColor("#1d3557")   # deep navy — headings, header bg
    C_STEEL      = colors.HexColor("#457b9d")   # mid-steel — sub-headings, borders
    C_SKY        = colors.HexColor("#a8c8e1")   # pale sky — table zebra, light accents
    C_CLOUD      = colors.HexColor("#eef4f9")   # near-white blue — very light fills
    # Accent / alert
    C_TEAL       = colors.HexColor("#2a9d8f")   # teal — callout borders
    C_TEAL_BG    = colors.HexColor("#e8f6f4")   # teal tint — callout background
    C_ALERT      = colors.HexColor("#e63946")   # action-red — urgent flags
    C_ALERT_BG   = colors.HexColor("#fde8e8")   # red tint — urgent row highlights
    # Neutral
    C_INK        = colors.HexColor("#1c2b3a")   # near-black — body text
    C_MUTED      = colors.HexColor("#5a7080")   # medium grey — captions, secondary
    C_RULE       = colors.HexColor("#c5d5e4")   # light rule — table grid lines
    C_WHITE      = colors.white

    # ── Heading overrides ─────────────────────────────────────────────────────
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_CENTER

    styles["Title"].fontSize = 22
    styles["Title"].textColor = C_NAVY
    styles["Title"].fontName  = "Helvetica-Bold"
    styles["Title"].leading   = 26
    styles["Title"].spaceAfter = 4

    styles["Heading2"].fontSize    = 13
    styles["Heading2"].textColor   = C_NAVY
    styles["Heading2"].fontName    = "Helvetica-Bold"
    styles["Heading2"].leading     = 16
    styles["Heading2"].spaceBefore = 10
    styles["Heading2"].spaceAfter  = 4
    styles["Heading2"].keepWithNext = 1

    styles["Heading3"].fontSize    = 10.5
    styles["Heading3"].textColor   = C_STEEL
    styles["Heading3"].fontName    = "Helvetica-Bold"
    styles["Heading3"].leading     = 14
    styles["Heading3"].spaceBefore = 8
    styles["Heading3"].spaceAfter  = 3
    styles["Heading3"].keepWithNext = 1

    styles["Heading4"].fontSize    = 9
    styles["Heading4"].textColor   = C_MUTED
    styles["Heading4"].fontName    = "Helvetica-Oblique"
    styles["Heading4"].leading     = 12
    styles["Heading4"].spaceBefore = 5
    styles["Heading4"].spaceAfter  = 2
    styles["Heading4"].keepWithNext = 1

    # ── Custom paragraph styles ───────────────────────────────────────────────
    caption_style = ParagraphStyle(
        "Caption",
        parent=styles["Normal"],
        fontSize=7.2,
        textColor=C_MUTED,
        leading=10,
        spaceAfter=3,
        spaceBefore=1,
        italic=True,
    )
    callout_style = ParagraphStyle(
        "Callout",
        parent=styles["Normal"],
        fontSize=8.8,
        textColor=colors.HexColor("#0f3d35"),
        backColor=C_TEAL_BG,
        borderColor=C_TEAL,
        borderWidth=1.0,
        borderPadding=(5, 8, 5, 8),
        leading=13,
        spaceAfter=6,
    )
    interp_style = ParagraphStyle(
        "Interp",
        parent=styles["Normal"],
        fontSize=8.5,
        textColor=C_INK,
        leading=12,
        spaceAfter=4,
        alignment=TA_LEFT,
    )
    small_style = ParagraphStyle(
        "Small",
        parent=styles["Normal"],
        fontSize=7.4,
        textColor=C_MUTED,
        leading=10,
        spaceAfter=3,
    )
    section_note_style = ParagraphStyle(
        "SectionNote",
        parent=styles["Normal"],
        fontSize=8,
        textColor=C_NAVY,
        backColor=C_CLOUD,
        borderColor=C_STEEL,
        borderWidth=0.8,
        borderPadding=(4, 7, 4, 7),
        leading=12,
        spaceAfter=5,
    )
    cell_hdr_style = ParagraphStyle(
        "CellHdr",
        parent=styles["Normal"],
        fontSize=7.8,
        textColor=C_WHITE,
        fontName="Helvetica-Bold",
        leading=11,
        wordWrap="LTR",
        splitLongWords=False,
        hyphenationLang="",
        spaceAfter=0,
    )
    cell_body_style = ParagraphStyle(
        "CellBody",
        parent=styles["Normal"],
        fontSize=7.4,
        textColor=C_INK,
        leading=10,
        wordWrap="LTR",
        splitLongWords=False,
        hyphenationLang="",
        spaceAfter=0,
    )
    cell_bold_style = ParagraphStyle(
        "CellBold",
        parent=styles["Normal"],
        fontSize=7.4,
        fontName="Helvetica-Bold",
        textColor=C_INK,
        leading=10,
        spaceAfter=0,
    )
    card_title_style = ParagraphStyle(
        "DashboardCardTitle",
        parent=styles["Normal"],
        fontSize=6.8,
        fontName="Helvetica-Bold",
        textColor=C_MUTED,
        leading=8.2,
        spaceAfter=1,
    )
    card_value_style = ParagraphStyle(
        "DashboardCardValue",
        parent=styles["Normal"],
        fontSize=11,
        fontName="Helvetica-Bold",
        textColor=C_NAVY,
        leading=13,
        spaceAfter=1,
    )
    card_note_style = ParagraphStyle(
        "DashboardCardNote",
        parent=styles["Normal"],
        fontSize=6.6,
        textColor=C_INK,
        leading=8.2,
        spaceAfter=0,
    )

    story = []
    table_counter = {"value": 0}

    def append_numbered_caption(caption_text: str, *, style=caption_style) -> None:
        table_counter["value"] += 1
        caption_para = Paragraph(_format_table_caption(caption_text, table_counter["value"]), style)
        # Keep caption on the same page as the preceding table/content
        if story and not isinstance(story[-1], Spacer):
            last = story.pop()
            story.append(KeepTogether([last, caption_para]))
        else:
            story.append(caption_para)

    def append_table_with_caption(
        table: Table,
        caption_text: str | None = None,
        *,
        spacer_after: float = 0.15,
        keep_together: bool = True,
        max_keep_rows: int = 30,
    ) -> None:
        """Keep table/caption together when practical to reduce page-split artifacts."""
        parts = [table]
        if caption_text:
            table_counter["value"] += 1
            parts.append(Paragraph(_format_table_caption(caption_text, table_counter["value"]), caption_style))

        row_count = getattr(table, "_nrows", 0)
        if keep_together and row_count and row_count <= max_keep_rows:
            story.append(KeepTogether(parts))
        else:
            for part in parts:
                story.append(part)

        if spacer_after > 0:
            story.append(Spacer(1, spacer_after * inch))

    def append_appendix_table_block(heading: str, table: Table, caption: str, note: str | None = None) -> None:
        """Keep appendix heading, optional note, table, and fixed caption together when practical."""
        parts = [Paragraph(heading, styles["Heading3"])]
        if note:
            parts.append(Paragraph(note, small_style))
            parts.append(Spacer(1, 0.04 * inch))
        parts.extend([table, Paragraph(caption, caption_style)])
        row_count = getattr(table, "_nrows", 0)
        if row_count and row_count <= 18:
            story.append(KeepTogether(parts))
        else:
            for part in parts:
                story.append(part)
        story.append(Spacer(1, 0.12 * inch))

    pair_id_style = ParagraphStyle(
        "PairId",
        parent=styles["Normal"],
        fontName="Courier",
        fontSize=6.5,
        leading=9,
        textColor=colors.HexColor("#2d3a4a"),
        spaceAfter=0,
    )

    def wrap_cell(value: object, style=cell_body_style):
        return Paragraph(str(value), style)

    def wrap_rows(rows: list[list[object]], header_style=cell_hdr_style, body_style=cell_body_style):
        wrapped_rows = []
        for row_index, row in enumerate(rows):
            style = header_style if row_index == 0 else body_style
            wrapped_rows.append([
                cell if isinstance(cell, Paragraph) else wrap_cell(cell, style)
                for cell in row
            ])
        return wrapped_rows

    TABLE_HEADER_BG   = C_NAVY
    TABLE_HEADER_TEXT = C_WHITE
    TABLE_BORDER      = C_STEEL
    TABLE_GRID        = C_RULE
    TABLE_ZEBRA       = C_CLOUD   # alternating row fill

    def standard_table_style(
        font_size: float,
        header: bool = True,
        valign_top: bool = False,
        zebra: bool = True,
        dense: bool = False,
    ):
        """Shared table styling with lighter grids for dense operational layouts."""
        commands = [
            ("BOX",       (0, 0), (-1, -1), 0.55, TABLE_BORDER),
            ("INNERGRID", (0, 0), (-1, -1), 0.12 if dense else 0.18, TABLE_GRID),
            ("FONTNAME",  (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE",  (0, 0), (-1, -1), font_size),
            ("TOPPADDING",    (0, 0), (-1, -1), 2 if dense else 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2 if dense else 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 4),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ]
        if header:
            commands.extend([
                ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEADER_BG),
                ("TEXTCOLOR",  (0, 0), (-1, 0), TABLE_HEADER_TEXT),
                ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
                ("TOPPADDING",    (0, 0), (-1, 0), 4),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
                # Accent rule below header row
                ("LINEBELOW",  (0, 0), (-1, 0), 1.2, C_STEEL),
            ])
        if zebra:
            # Stripe every even data row (rows 2, 4, 6, …; row 0 = header)
            commands.append(("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [C_WHITE, TABLE_ZEBRA]))
        if valign_top:
            commands.append(("VALIGN", (0, 0), (-1, -1), "TOP"))
        return TableStyle(commands)

    def section_divider(label: str | None = None, *, min_following_height: float = 2.35) -> None:
        """Append a section break with orphan control for professional report flow.

        ReportLab's paragraph-level ``keepWithNext`` cannot protect a compound
        section opener (rule + label + heading) from landing at the bottom of a
        page. A conditional page break reserves enough space for the opener and
        first substantive content block, preventing disconnected headings and
        excessive whitespace in mobile PDF viewers.
        """
        from reportlab.platypus import HRFlowable

        story.append(CondPageBreak(min_following_height * inch))
        rule = HRFlowable(width="100%", thickness=1.2, color=C_NAVY, spaceAfter=0, spaceBefore=0)
        if label:
            label_para = Paragraph(
                f"<b>{label}</b>",
                ParagraphStyle(
                    "_DividerLabel",
                    parent=styles["Normal"],
                    fontSize=9.5,
                    textColor=C_NAVY,
                    fontName="Helvetica-Bold",
                    spaceBefore=3,
                    spaceAfter=2,
                    leading=13,
                    keepWithNext=1,
                ),
            )
            story.append(KeepTogether([Spacer(1, 0.08 * inch), rule, label_para]))
        else:
            story.append(KeepTogether([Spacer(1, 0.08 * inch), rule, Spacer(1, 0.04 * inch)]))

    def append_section_heading(title: str, *, min_following_height: float = 1.55) -> None:
        """Append a Heading3 with enough remaining frame space for its first block."""
        story.append(CondPageBreak(min_following_height * inch))
        story.append(Paragraph(title, styles["Heading3"]))

    story.append(Paragraph("NI TB Genomic Surveillance", styles["Title"]))


    def fit_col_widths(widths, fill=True, page_width=7.375 * inch):
        total = sum(widths)
        if fill and total > 0:
            factor = page_width / total
            return [w * factor for w in widths]
        return widths

    def dashboard_card(title: str, value: str, note: str):
        return [
            Paragraph(title.upper(), card_title_style),
            Paragraph(value, card_value_style),
            Paragraph(note, card_note_style),
        ]

    def dashboard_table(cards: list, columns: int = 4) -> Table:
        rows = []
        for idx in range(0, len(cards), columns):
            row = cards[idx:idx + columns]
            while len(row) < columns:
                row.append("")
            rows.append(row)

        tbl = Table(
            rows,
            colWidths=fit_col_widths([1.84 * inch] * columns, fill=True),
            hAlign="LEFT",
        )
        tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, -1), C_CLOUD),
            ("BOX", (0, 0), (-1, -1), 0.4, C_RULE),
            ("INNERGRID", (0, 0), (-1, -1), 3.0, C_WHITE),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        return tbl

    story.append(Paragraph("Outbreak Investigation Report", styles["Heading2"]))
    story.append(Paragraph(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC", styles["Normal"]))
    story.append(Spacer(1, 0.18 * inch))

    summary_total_cases = int(kpi_data.get("eligible_cases", total_cases) if kpi_data else total_cases)
    summary_sequenced_cases = int(kpi_data.get("sequenced_cases", len(sequence_by_case)) if kpi_data else len(sequence_by_case))
    summary_coverage = (
        f"{float(kpi_data.get('sequenced_pct')):.1f}%" if kpi_data and kpi_data.get("sequenced_pct") is not None
        else (f"{(summary_sequenced_cases / summary_total_cases) * 100.0:.1f}%" if summary_total_cases else "n/a")
    )
    summary_qc_pass_rate = (
        f"{float(kpi_data.get('qc_pass_pct')):.1f}%" if kpi_data and kpi_data.get("qc_pass_pct") is not None
        else (f"{(qc_status_counts['pass'] / max(qc_status_counts['pass'] + qc_status_counts['fail'], 1)) * 100.0:.1f}%" if (qc_status_counts['pass'] + qc_status_counts['fail']) else "n/a")
    )
    model_reliability = "Exploratory"
    if summary_data and summary_data.get("convergence_diagnostic") is not None:
        try:
            model_reliability = "Operationally stronger" if float(summary_data["convergence_diagnostic"]) <= 1.1 else "Exploratory"
        except Exception:
            model_reliability = "Exploratory"

    required_repro_metadata = [
        ("Reference genome", summary_data.get("reference_genome") if summary_data else None),
        ("SNP-calling pipeline/version", summary_data.get("snp_pipeline_version") if summary_data else None),
        ("Resistance catalogue/version", lineage_dr_data.get("resistance_catalogue_version") if lineage_dr_data else None),
        ("Lineage-calling tool/version", lineage_dr_data.get("lineage_tool_version") if lineage_dr_data else None),
        ("outbreaker2 version", summary_data.get("analysis_engine_version") if summary_data else None),
        ("Random seed", summary_data.get("random_seed") if summary_data else None),
    ]
    missing_required_repro_fields = [
        label
        for label, value in required_repro_metadata
        if value is None or (isinstance(value, str) and not value.strip())
    ]
    circulation_label = "Development / Internal Draft Only" if missing_required_repro_fields else "Eligible for External Circulation"
    high_priority_open_clusters = sum(
        1
        for row in cluster_action_rows
        if str(row.get("investigation_status") or "").lower() == "open" and int(row.get("priority_score") or 0) > 10
    ) if cluster_action_rows else 0

    executive_dashboard_cards = [
        dashboard_card(
            "Circulation",
            "Draft blocked" if missing_required_repro_fields else "Governance ready",
            "Populate mandatory reproducibility fields" if missing_required_repro_fields else "Metadata gate passed",
        ),
        dashboard_card(
            "QC unresolved",
            str(qc_status_counts["fail"] + qc_status_counts["not_reported"] + qc_status_counts["contamination"]),
            f"{qc_status_counts['pass']} pass; {summary_qc_pass_rate} pass rate",
        ),
        dashboard_card(
            "SNP-supported links",
            str(pairwise_links_le_12),
            "≤12 SNP candidate links before epi review",
        ),
        dashboard_card(
            "Open clusters",
            str(open_clusters),
            f"{high_priority_open_clusters} high-priority (>10)",
        ),
        dashboard_card(
            "Model links",
            str(high_confidence_all_count),
            "Posterior ≥0.70; validate with SNP + epi",
        ),
        dashboard_card(
            "MDR/RR signals",
            str(len(mutation_validation_rows)),
            "Phenotypic DST confirmation required",
        ),
        dashboard_card(
            "Model reliability",
            model_reliability,
            "Directionality remains cautious if exploratory",
        ),
        dashboard_card(
            "Coverage",
            summary_coverage,
            f"{summary_sequenced_cases}/{summary_total_cases} eligible cases sequenced",
        ),
    ]

    if missing_required_repro_fields:
        story.append(Paragraph(
            "<b>REPORT STATUS: DEVELOPMENT / INTERNAL DRAFT ONLY</b> - Required reproducibility metadata is incomplete; external circulation is blocked until mandatory fields are populated.",
            section_note_style,
        ))
    append_section_heading("Executive Action Summary", min_following_height=2.1)
    story.append(Paragraph(
        "Use this page first. Items marked model-only or exploratory require genomic validation and epidemiological corroboration before operational action.",
        section_note_style,
    ))
    story.append(dashboard_table(executive_dashboard_cards))
    story.append(Spacer(1, 0.1 * inch))

    _top_action_reasons = (
        f"1 \u2014 QC: {qc_status_counts['fail']} low-coverage fails, "
        f"{qc_status_counts['not_reported']} QC not-reported, "
        f"{qc_status_counts['contamination']} contamination flags.  "
        "2 \u2014 Resistance: Gene-drug combinations are preliminary; verify against WHO catalogue.  "
        "3 \u2014 DST: MDR/RR genomic signals require culture-based confirmation before clinical action.  "
        f"4 \u2014 Clusters: {int(open_clusters or 0)} clusters remain open for epidemiological investigation.  "
        "5 \u2014 Model links: No pairwise SNP \u226412 direct-transmission support in this extract."
    )
    top_actions_due_rows = [
        ["Priority", "Action", "Owner", "Due"],
        ["1", "Repeat sequencing / QC review", "Laboratory", "48 h"],
        ["2", "Validate drug-resistance pipeline mapping", "Bioinformatics / Microbiology", "Immediate"],
        ["3", "Confirm phenotypic DST", "TB Microbiology / MDT", "Immediate"],
        ["4", "Review open genomic clusters", "TB MDT / PHA", "Next MDT"],
        ["5", "Do not escalate model-only links to field", "HPT / TB Nurses", "Ongoing"],
    ]
    top_actions_table = Table(
        wrap_rows(top_actions_due_rows),
        colWidths=fit_col_widths([0.62 * inch, 2.9 * inch, 2.0 * inch, 0.9 * inch], fill=True),
        repeatRows=1,
    )
    top_actions_table.setStyle(standard_table_style(font_size=7.5, header=True, valign_top=True))
    story.append(Spacer(1, 0.08 * inch))
    append_section_heading("Top Actions Due Now")
    append_table_with_caption(
        top_actions_table,
        "Operational worklist for immediate MDT actioning.",
        spacer_after=0.05,
    )
    story.append(Paragraph(f"<b>Reasons:</b> {_top_action_reasons}", small_style))
    story.append(PageBreak())

    # ── MDT Governance Summary (page 2) ───────────────────────────────────────
    append_section_heading("MDT Governance Summary")
    _early_hpc = sum(
        1
        for row in cluster_action_rows
        if str(row.get("investigation_status") or "").lower() == "open" and int(row.get("priority_score") or 0) > 10
    ) if cluster_action_rows else 0
    _early_mdt_rows = [
        ["Priority area", "Current signal", "Required MDT action", "Owner", "When"],
        ["Circulation readiness", circulation_label, "Complete mandatory reproducibility metadata before external circulation", "Pipeline + Governance", "Before circulation"],
        ["Model reliability", model_reliability, "Treat directionality as exploratory; diagnostics unavailable", "Bioinformatics + MDT", "Current cycle"],
        ["Open clusters", f"{int(open_clusters or 0)} total / {_early_hpc} priority >10", "MDT review and epi data completion for all open clusters", "MDT + Field team", "Next MDT"],
        ["Discordant model links", "see Appendix B", "Pairwise SNP + epi adjudication pathway", "MDT", "Next MDT"],
        ["Geography completeness", "UK-only extract", "Re-ingest with HSC Trust / PHA locality / council area", "Data management", "Next ingest"],
    ]
    _early_mdt_table = Table(
        wrap_rows(_early_mdt_rows),
        colWidths=fit_col_widths([1.5 * inch, 1.2 * inch, 2.85 * inch, 1.0 * inch, 0.9 * inch], fill=True),
        repeatRows=1,
    )
    _early_mdt_table.setStyle(standard_table_style(font_size=7.4, header=True, valign_top=True))
    story.append(_early_mdt_table)
    story.append(Paragraph(
        "Full MDT action sheet with final discordant-pair counts is in <b>Appendix C</b>.",
        small_style,
    ))
    story.append(Spacer(1, 0.2 * inch))

    # ── Table of Contents ─────────────────────────────────────────────────────
    append_section_heading("Contents")
    _toc_rows = [
        ["Section", "Content", "Location"],
        ["Executive Action Summary", "Circulation status, QC, resistance, cluster, model risk dashboard", "Opening"],
        ["Top Actions Due Now", "Immediate operational worklist", "Opening"],
        ["MDT Governance Summary", "Condensed governance action sheet", "Opening"],
        ["About This Report / Analysis Summary", "Programme context, KPIs, sequencing summary", "Main"],
        ["Case-Level Operational Actions", "Per-case cluster assignment and action table", "Main"],
        ["Model-Prioritised Transmission Hypotheses", "outbreaker2 posterior-probability pair tables", "Main"],
        ["Pairwise SNP Distance Summary", "SNP distance bands for all model-prioritised pairs", "Main"],
        ["Current Outbreak Interpretation", "Narrative interpretation and discordance review", "Main"],
        ["QC Failure Drill-Down", "Per-sample QC detail and low-coverage cases", "Main"],
        ["Run-Level QC Summary", "Sequencing run quality by report date; pending run IDs where unavailable", "Main"],
        ["Drug-Resistance Mutation Details", "Gene-drug reference and resistance mutation evidence", "Main"],
        ["Phenotypic DST Reconciliation", "Genomic vs phenotypic DST comparison; pending lab data where unavailable", "Main"],
        ["Cluster Epidemiology Summary", "Genomic summary + operational tracker for all clusters", "Main"],
        ["Cluster Growth / Epi Evidence / Geography", "Growth, corroboration, contact tracing, spread and missing-data dashboards", "Main"],
        ["Cluster Prioritisation / Model Diagnostics / Figures", "Priority table, MCMC diagnostics, network graphics", "Main"],
        ["Data Provenance and Reproducibility", "Software versions, run metadata, SHA-256 input hashes", "Main"],
        ["Appendix A", "Case classification (A1a) and case actions (A1b)", "Appendix"],
        ["Appendix B", "Full discordance review (coded table)", "Appendix"],
        ["Appendix C", "Printable MDT action sheet for governance circulation", "Appendix"],
        ["Appendix D", "TB Genomics Key Concepts reference", "Appendix"],
    ]
    _toc_table = Table(
        wrap_rows(_toc_rows),
        colWidths=fit_col_widths([2.1 * inch, 4.35 * inch, 0.9 * inch], fill=True),
        repeatRows=1,
    )
    _toc_table.setStyle(standard_table_style(font_size=7.4, header=True, valign_top=False))
    story.append(_toc_table)
    story.append(Spacer(1, 0.05 * inch))
    story.append(Paragraph(
        "Appendices A\u2013D are in landscape format after the main report. "
        "Flag codes used in pair tables: SNP-linked = SNP \u226412 supported; D1: SNP>12 = genomically discordant; Model-only = no pairwise SNP; QC-unresolved = QC failed/not reported.",
        small_style,
    ))
    story.append(PageBreak())

    # ── About This Report ──────────────────────────────────────────────────────
    append_section_heading("About This Report", min_following_height=1.8)
    story.append(Paragraph(
        "This report is produced by the Northern Ireland TB Genomic Surveillance platform using whole-genome sequencing (WGS) "
        "data and epidemiological case records. It is intended to support TB programme staff and public health investigators "
        "by providing genomic evidence for transmission clusters, drug-resistance profiles, and programme performance metrics. "
        "<b>This is a decision-support tool only — all findings must be reviewed and acted on by a qualified clinician or "
        "public health professional. No automated decisions are made.</b>",
        interp_style,
    ))
    story.append(Spacer(1, 0.1 * inch))

    # ── TB Genomics Background — stored for Appendix D ─────────────────────────
    story.append(Paragraph(
        "<b>Genomic terminology:</b> For definitions of WGS, SNP, cluster, outbreaker2, MCMC convergence, and other terms "
        "used in this report, see <b>Appendix D: TB Genomics Key Concepts</b> at the end of this document.",
        small_style,
    ))
    story.append(Spacer(1, 0.12 * inch))
    _key_concepts_rows = [
        [Paragraph("Concept", cell_hdr_style), Paragraph("Explanation", cell_hdr_style)],
        [Paragraph("Whole-Genome Sequencing (WGS)", cell_bold_style),
         Paragraph("Reads the complete ~4.4 Mb genome of M. tuberculosis. More informative than conventional typing (MIRU, spoligotyping).", cell_body_style)],
        [Paragraph("SNP (single nucleotide polymorphism)", cell_bold_style),
         Paragraph("A single base-pair difference in the genome. Closely related strains share very few SNPs. Used as a genetic \u2018distance\u2019 metric.", cell_body_style)],
        [Paragraph("SNP threshold for transmission", cell_bold_style),
         Paragraph("Strains with \u226412 SNPs are considered potentially linked (UK NICE guidance). \u22645 SNPs suggests recent direct transmission. "
                   ">50 SNPs effectively rules out recent shared transmission.", cell_body_style)],
        [Paragraph("Lineage", cell_bold_style),
         Paragraph("M. tuberculosis is classified into 7+ major lineages (L1\u2013L7) reflecting global evolutionary history. Lineage influences "
                   "drug-resistance patterns and may correlate with transmissibility.", cell_body_style)],
        [Paragraph("Cluster", cell_bold_style),
         Paragraph("A group of cases whose sequences are genetically similar (within the SNP threshold). A cluster does not prove "
                   "direct person-to-person transmission \u2014 epidemiological linkage is required to confirm transmission routes.", cell_body_style)],
        [Paragraph("outbreaker2", cell_bold_style),
         Paragraph("A Bayesian MCMC method that combines SNP distances with collection dates and an assumed generation time to probabilistically "
                   "infer who-infected-whom. Output posterior probabilities indicate the likelihood of a direct transmission event between any pair of cases.", cell_body_style)],
        [Paragraph("Generation time", cell_bold_style),
         Paragraph("The average time between one person being infected and the next person they infect being detected. "
                   "For TB, this is typically 1\u20133 years (range 0.5\u20135 years) due to the long latency period.", cell_body_style)],
        [Paragraph("MCMC convergence", cell_bold_style),
         Paragraph("Markov Chain Monte Carlo simulations must reach a stable state (\u2018converge\u2019). A convergence diagnostic near 1.0 "
                   "indicates reliable estimates. Values >1.1 suggest the chain has not fully mixed and results should be interpreted cautiously.", cell_body_style)],
        [Paragraph("Drug resistance", cell_bold_style),
         Paragraph("Genomic mutations predict resistance to first-line drugs (isoniazid, rifampicin, etc.) and define MDR-TB (multi-drug resistant) "
                   "and XDR-TB (extensively drug resistant). Genomic DR prediction is used alongside phenotypic DST.", cell_body_style)],
        [Paragraph("Transmission network", cell_bold_style),
         Paragraph("A directed graph where arrows indicate the most probable direction of transmission. Model-prioritised links "
                   "(posterior probability >0.70) are hypotheses and should be validated against pairwise SNP, QC status, and epidemiology.", cell_body_style)],
    ]

    append_section_heading("Case Summary", min_following_height=1.9)
    summary_table_data = [
        ["Total Cases", str(int(total_cases))],
        ["Clustered Cases", str(int(clustered_cases))],
        ["Unclustered Cases", str(int(total_cases) - int(clustered_cases))],
        ["Open Clusters", str(int(open_clusters))],
    ]
    summary_table = Table(wrap_rows(summary_table_data, header_style=cell_body_style, body_style=cell_body_style), colWidths=[2.4 * inch, 1.5 * inch])
    summary_table.setStyle(standard_table_style(font_size=9.2, header=False))
    append_table_with_caption(
        summary_table,
        "Table 2. Programme case count summary. Clustered cases are those linked by genomic similarity to at least one other case. "
        "Unclustered (singleton) cases may represent imported strains, sporadic transmission, or reactivation of latent disease. "
        "Open clusters are active genomic transmission clusters with ongoing epidemiological investigation.",
        spacer_after=0.15,
    )

    interpretation_flags = []
    if kpi_data:
        sequenced_pct = kpi_data.get("sequenced_pct")
        qc_pass_pct = kpi_data.get("qc_pass_pct")
        contamination_flags = int(kpi_data.get("contamination_flag_cases") or 0)
        qc_turnaround = kpi_data.get("median_days_specimen_to_qc")

        if sequenced_pct is not None and float(sequenced_pct) < 80.0:
            interpretation_flags.append(
                f"Sequencing coverage is below target: {sequenced_pct}% (target >= 80%)."
            )
        if qc_pass_pct is not None and float(qc_pass_pct) < 90.0:
            interpretation_flags.append(
                f"QC pass rate is below target: {qc_pass_pct}% (target >= 90%)."
            )
        if contamination_flags > 0:
            interpretation_flags.append(
                f"Contamination flags detected: {contamination_flags} cases require review."
            )
        if qc_turnaround is not None and float(qc_turnaround) > 21.0:
            interpretation_flags.append(
                f"Median specimen-to-QC turnaround is elevated: {qc_turnaround} days."
            )
    if int(open_clusters or 0) > 0:
        interpretation_flags.append(f"Open clusters requiring investigation: {int(open_clusters)}.")
    if high_confidence_all_count > 0:
        interpretation_flags.append(
            "Model-prioritised transmission hypotheses present; validate before field escalation."
        )

    append_section_heading("Automated Interpretation Flags", min_following_height=1.4)
    if interpretation_flags:
        for flag in interpretation_flags:
            story.append(Paragraph(f"- {flag}", styles["Normal"]))
    else:
        story.append(Paragraph("No elevated operational risk flags detected in current report window.", styles["Normal"]))

    section_divider("Analysis context", min_following_height=3.25)

    append_section_heading("Analysis Summary")
    if summary_data:
        analysis_label_map = {
            "n_samples": "Posterior Samples",
            "n_generations": "MCMC Iterations",
            "n_iter": "MCMC Iterations",
            "burnin": "Burn-in",
            "likelihood_mean": "Mean Log-Likelihood",
            "likelihood_sd": "Log-Likelihood SD",
            "transmission_probability": "Transmission Probability",
            "generation_time_mean": "Generation Time Mean (days)",
            "generation_time_sd": "Generation Time SD (days)",
            "sampling_probability": "Sampling Probability",
            "convergence_diagnostic": "Convergence Diagnostic",
        }

        ordered_keys = [
            "n_samples",
            "n_generations",
            "n_iter",
            "burnin",
            "likelihood_mean",
            "likelihood_sd",
            "transmission_probability",
            "generation_time_mean",
            "generation_time_sd",
            "sampling_probability",
            "convergence_diagnostic",
        ]

        def format_metric_value(value):
            if isinstance(value, float):
                return f"{value:.3f}" if abs(value) < 10 else f"{value:.2f}"
            return str(value)

        analysis_rows = []
        for key in ordered_keys:
            if key in summary_data:
                analysis_rows.append([analysis_label_map.get(key, key), format_metric_value(summary_data[key])])
        if analysis_rows:
            analysis_table = Table(wrap_rows(analysis_rows, header_style=cell_body_style, body_style=cell_body_style), colWidths=[2.4 * inch, 3.4 * inch])
            analysis_table.setStyle(standard_table_style(font_size=9.0, header=False))
            append_table_with_caption(analysis_table, spacer_after=0.0)
        else:
            story.append(Paragraph("No structured analysis metrics available.", styles["Normal"]))
    else:
        story.append(Paragraph("No outbreak summary JSON found.", styles["Normal"]))

    append_numbered_caption(
        "Table 3. outbreaker2 Bayesian MCMC analysis parameters. "
        "<b>Posterior Samples</b> is the number of accepted MCMC draws used to compute estimates — higher values give more stable posteriors. "
        "<b>Mean Log-Likelihood</b> reflects model fit; values closer to zero (less negative) indicate better fit. "
        "<b>Transmission Probability</b> is the average posterior probability that any given case-pair represents a direct transmission event. "
        "<b>Generation Time</b> is the modelled average interval (in days) between infection events in a transmission chain. "
        "<b>Convergence Diagnostic</b> near 1.0 confirms the MCMC chain has stabilised; values >1.1 indicate cautious interpretation is needed."
    )
    story.append(Paragraph(
        "<b>How to interpret outbreaker2 results:</b> outbreaker2 reconstructs the most probable transmission tree using "
        "both genetic distance (SNPs) and timing (collection dates). Pairs with high posterior transmission probability "
        "are model-prioritised transmission hypotheses only. In this report, they should not be interpreted as direct "
        "transmission unless supported by pairwise SNP distance ≤12, QC pass status, and epidemiological corroboration. "
        "Lower probability pairs may still be linked within the same cluster but through one or more undetected intermediate cases.",
        section_note_style,
    ))

    section_divider("Sequencing and QC", min_following_height=3.0)
    append_section_heading("Programme Surveillance KPIs (Last 12 Weeks)")
    if kpi_data:
        kpi_table_data = [
            ["Eligible Cases", str(kpi_data.get("eligible_cases", 0))],
            ["Sequenced Cases", str(kpi_data.get("sequenced_cases", 0))],
            ["Sequencing Coverage (%)", str(kpi_data.get("sequenced_pct", "n/a"))],
            ["QC Reported Cases", str(kpi_data.get("qc_reported_cases", 0))],
            ["QC Pass Cases", str(kpi_data.get("qc_pass_cases", 0))],
            ["QC Fail Cases", str(kpi_data.get("qc_fail_cases", 0))],
            ["QC Pass Rate (%)", str(kpi_data.get("qc_pass_pct", "n/a"))],
            ["Contamination Flags", str(kpi_data.get("contamination_flag_cases", 0))],
            ["Median Days Specimen to QC", str(kpi_data.get("median_days_specimen_to_qc", "n/a"))],
        ]
        kpi_table = Table(wrap_rows(kpi_table_data, header_style=cell_body_style, body_style=cell_body_style), colWidths=[2.8 * inch, 3.0 * inch])
        kpi_table.setStyle(standard_table_style(font_size=9.0, header=False))
        append_table_with_caption(kpi_table, spacer_after=0.0)

        if kpi_data.get("warning"):
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(f"KPI Warning: {kpi_data['warning']}", styles["Italic"]))

        append_numbered_caption(
            "Table 4. Programme surveillance KPIs over the reporting window. "
            "<b>Sequencing Coverage</b> is the percentage of eligible TB culture-confirmed cases that have received whole-genome sequencing. "
            "The UK target is ≥80%. "
            "<b>QC Pass Rate</b> is the percentage of sequenced samples that meet quality thresholds (e.g. ≥95% genome coverage at ≥10×). "
            "Low pass rates may indicate DNA quality issues, contamination, or laboratory process variation. "
            "<b>Contamination Flags</b> indicate samples where a mixed-strain signal suggests cross-contamination requiring repeat or rejection."
        )

        representativeness = kpi_data.get("representativeness_by_region") or []
        if representativeness:
            story.append(Spacer(1, 0.15 * inch))
            story.append(Paragraph("Regional Sequencing Representativeness", styles["Heading4"]))
            region_rows = [["Region", "Eligible", "Sequenced", "Coverage %"]]
            for row in representativeness[:8]:
                region_rows.append([
                    str(row.get("region", "Unknown")),
                    str(row.get("eligible_cases", 0)),
                    str(row.get("sequenced_cases", 0)),
                    str(row.get("sequenced_pct", "n/a")),
                ])
            region_table = Table(wrap_rows(region_rows), colWidths=[2.1 * inch, 1.1 * inch, 1.1 * inch, 1.1 * inch])
            region_table.setStyle(standard_table_style(font_size=8.4, header=True))
            append_table_with_caption(region_table, spacer_after=0.0)
    else:
        story.append(Paragraph(
            "Full surveillance KPI artifact unavailable at report generation time. "
            "However, high-level KPI summary is available from the Executive Action Summary: "
            f"Sequencing coverage {summary_coverage}, QC pass rate {summary_qc_pass_rate}, "
            f"Contamination flags {qc_status_counts['contamination']}, Open clusters {open_clusters}. "
            "For complete regional and temporal KPI trends, check the surveillance system directly.",
            styles["Normal"],
        ))

    append_numbered_caption(
        "Table 5. Regional sequencing representativeness. Regions with coverage <80% may introduce ascertainment bias — "
        "clusters in under-sequenced regions may be underdetected. Where persistent regional gaps exist, "
        "review laboratory submission pathways and specimen transport processes."
    )
    section_divider(min_following_height=3.25)
    append_section_heading("Weekly Surveillance Trends (12 Weeks)")
    if weekly_trends:
        trend_rows = [["Week", "Eligible", "Sequenced", "Coverage %", "QC Pass %"]]
        for row in weekly_trends:
            trend_rows.append(
                [
                    str(row.get("week_start", "")),
                    str(row.get("eligible_cases", 0)),
                    str(row.get("sequenced_cases", 0)),
                    str(row.get("sequenced_pct", "n/a")),
                    str(row.get("qc_pass_pct", "n/a")),
                ]
            )
        trend_table = Table(wrap_rows(trend_rows), colWidths=[1.35 * inch, 1.0 * inch, 1.0 * inch, 1.0 * inch, 1.0 * inch])
        trend_table.setStyle(standard_table_style(font_size=7.8, header=True))
        story.append(trend_table)
        append_numbered_caption(
            "Weekly sequencing coverage and QC pass rates over the last 12 weeks. "
            "A declining trend may indicate emerging laboratory issues. "
            "Weeks with zero eligible cases may reflect reporting lags rather than true absence of TB."
        )

        trend_chart_path = build_trend_chart()
        if trend_chart_path and os.path.exists(trend_chart_path):
            story.append(Spacer(1, 0.12 * inch))
            chart_img = build_report_image(trend_chart_path)
            fig1_cap = Paragraph(
                "Figure 1. 12-week surveillance trend — sequencing coverage (%) and QC pass rate (%) by "
                "calendar week. Coverage is the proportion of eligible culture-confirmed TB cases that received WGS. "
                "Declining coverage weeks may reflect specimen submission delays, laboratory capacity issues, "
                "or data processing backlogs. QC pass rate below 90% in consecutive weeks warrants a laboratory review.",
                caption_style,
            )
            story.append(KeepTogether([chart_img, fig1_cap]))
    else:
        story.append(Paragraph("Weekly trends unavailable.", styles["Normal"]))
    section_divider("Cluster operations")
    append_section_heading("Cluster Action Prioritization")
    if cluster_action_rows:
        action_rows = [["Cluster", "Cases", "Regions", "Most Recent", "Status", "Priority"]]
        for row in cluster_action_rows:
            action_rows.append(
                [
                    str(row.get("cluster_id", ""))[:8],
                    str(row.get("case_count", 0)),
                    str(row.get("region_count", 0)),
                    str(row.get("most_recent_specimen", "")),
                    str(row.get("investigation_status", "unknown")),
                    str(row.get("priority_score", 0)),
                ]
            )
        action_table = Table(wrap_rows(action_rows), colWidths=[1.1 * inch, 0.8 * inch, 0.85 * inch, 1.35 * inch, 1.0 * inch, 0.8 * inch])
        action_table.setStyle(standard_table_style(font_size=7.8, header=True))
        append_table_with_caption(action_table, spacer_after=0.08)
        append_numbered_caption(
            "Table 7. Clusters ranked by investigation priority score. Score is composite: cluster size (×2), "
            "cross-region spread (×3), specimen recency within 14/30/60 days (×3/2/1), open investigation status (×3). "
            "Higher scores indicate clusters warranting prioritised MDT review and data-completeness follow-up. "
            "<b>Status 'open'</b> means an active review is ongoing or recommended."
        )
        story.append(Paragraph(
            "<b>Recommended action:</b> For clusters with priority score >10 and status 'open', ensure MDT review and epidemiological data completion. "
            "Do not infer direct transmission or source-recipient direction from model output alone. "
            "Cross-region clusters should trigger coordination checks across NHS trust boundaries.",
            section_note_style,
        ))
    else:
        story.append(Paragraph("No cluster action data available.", styles["Normal"]))

    story.append(Spacer(1, 0.15 * inch))
    if lineage_dr_data:
        analysis_summary = _lineage_analysis_summary(db)
        analysis_epi_summary = _lineage_epi_summary(db)
        interpreted_samples = int(analysis_summary.get("interpreted_samples", 0) or 0)
        with_lineage = int(analysis_summary.get("samples_with_lineage", 0) or 0)
        with_resistance = int(analysis_summary.get("samples_with_resistance_calls", 0) or 0)
        lineage_coverage_pct = round((with_lineage / interpreted_samples) * 100.0, 2) if interpreted_samples else 0.0
        resistance_coverage_pct = round((with_resistance / interpreted_samples) * 100.0, 2) if interpreted_samples else 0.0
        lineage_rows = [
            ["Interpreted Samples", str(interpreted_samples)],
            ["Samples with Lineage", str(with_lineage)],
            ["Lineage Coverage (%)", str(lineage_coverage_pct)],
            ["Samples with Resistance Calls", str(with_resistance)],
            ["Resistance Coverage (%)", str(resistance_coverage_pct)],
            ["Any Resistance Signal", str(analysis_epi_summary.get("samples_with_any_resistance_signal", 0))],
            ["Rifampicin-Resistant (suspected)", str(analysis_epi_summary.get("rifampicin_resistant_suspected", 0))],
            ["Isoniazid-Resistant (suspected)", str(analysis_epi_summary.get("isoniazid_resistant_suspected", 0))],
            ["MDR (suspected)", str(analysis_epi_summary.get("mdr_suspected", 0))],
            ["FQ-Resistant (suspected)", str(analysis_epi_summary.get("fluoroquinolone_resistant_suspected", 0))],
        ]
        lineage_table = Table(wrap_rows(lineage_rows, header_style=cell_body_style, body_style=cell_body_style), colWidths=[2.8 * inch, 3.0 * inch])
        lineage_table.setStyle(standard_table_style(font_size=8.8, header=False))
        lineage_caption_para = Paragraph(
            _format_table_caption(
                "Lineage and drug-resistance epidemiology summary. "
                "Rows prioritize actionable burden indicators (coverage, resistance signal counts, and suspected RR/MDR/FQ resistance) "
                "to support triage and follow-up planning.",
                table_counter["value"] + 1,
            ),
            caption_style,
        )
        table_counter["value"] += 1
        story.append(KeepTogether([
            Paragraph("Lineage and Drug Resistance Validation", styles["Heading3"]),
            lineage_table,
            lineage_caption_para,
        ]))
        story.append(Spacer(1, 0.05 * inch))
        top_lineages = analysis_epi_summary.get("top_lineages") or []
        if top_lineages:
            top_text = ", ".join([f"{x.get('lineage')}: {x.get('count')}" for x in top_lineages[:4]])
            story.append(Paragraph(f"Top observed lineages: {top_text}", styles["Normal"]))
    else:
        story.append(Paragraph("No lineage/DR validation artifact found.", styles["Normal"]))

    section_divider()
    append_section_heading("Secondary Transmission Engines")
    story.append(Paragraph(
        "<b>QC Filtering Status:</b> The outbreaker2 network displayed below includes samples with reported QC status at run time. "
        "Samples with QC status 'fail' or 'not_reported' may be present in the graph. "
        "When using results operationally, assume all displayed transmission links are candidates pending post-hoc QC validation. "
        "Links involving QC-failed or not-reported samples should not be escalated to field investigation until repeat sequencing or QC review is complete.",
        interp_style,
    ))
    if secondary_validation_data:
        secondary_epi = _secondary_epi_summary(
            secondary_validation_data=secondary_validation_data,
            transmission_data=transmission_data,
            method_comparison_data=method_comparison_data,
        )
        secondary_rows = [
            ["Consensus State", str(secondary_epi.get("consensus_state", "unknown"))],
            ["Primary Network Available", str(secondary_epi.get("primary_network_available", False))],
            ["High-Confidence Links", str(secondary_epi.get("high_confidence_links", 0))],
            ["Priority Nodes Flagged", str(secondary_epi.get("priority_nodes_flagged", 0))],
            ["Cross-Method Precision", str(secondary_epi.get("pairwise_precision", 0.0))],
            ["Cross-Method Recall", str(secondary_epi.get("pairwise_recall", 0.0))],
            ["Cross-Method Jaccard", str(secondary_epi.get("pairwise_jaccard", 0.0))],
        ]
        secondary_table = Table(wrap_rows(secondary_rows, header_style=cell_body_style, body_style=cell_body_style), colWidths=[2.8 * inch, 3.0 * inch])
        secondary_table.setStyle(standard_table_style(font_size=8.8, header=False))
        append_table_with_caption(
            secondary_table,
            "Table 9. Secondary transmission evidence summary. This section reports actionable signals from network inference and "
            "cross-method agreement, focusing on whether transmission hypotheses are strong enough to prioritize field investigation.",
            spacer_after=0.0,
        )
        if int(secondary_epi.get("high_confidence_links", 0)) > 0:
            story.append(Paragraph(
                "Action signal: model-prioritised transmission hypotheses are present; prioritize validation and exposure verification for flagged nodes.",
                section_note_style,
            ))
    else:
        story.append(Paragraph("No secondary engine validation artifact found.", styles["Normal"]))

    story.append(Spacer(1, 0.15 * inch))
    if method_comparison_data:
        coverage = method_comparison_data.get("coverage") or {}
        agreement = method_comparison_data.get("agreement") or {}
        comp_rows = [
            ["Sequence Assigned Cases", str(coverage.get("sequence_assigned_cases", 0))],
            ["Outbreaker Assigned Cases", str(coverage.get("outbreaker_assigned_cases", 0))],
            ["Overlap Cases", str(coverage.get("overlap_cases", 0))],
            ["Pairwise Precision", str(round(float(agreement.get("pairwise_precision_outbreaker_vs_sequence", 0.0)), 3))],
            ["Pairwise Recall", str(round(float(agreement.get("pairwise_recall_outbreaker_vs_sequence", 0.0)), 3))],
            ["Pairwise Jaccard", str(round(float(agreement.get("pairwise_jaccard", 0.0)), 3))],
        ]
        comp_table = Table(wrap_rows(comp_rows, header_style=cell_body_style, body_style=cell_body_style), colWidths=[2.8 * inch, 3.0 * inch])
        comp_table.setStyle(standard_table_style(font_size=8.8, header=False))
        table_counter["value"] += 1
        story.append(KeepTogether([
            Paragraph("Cross-Method Clustering Comparison", styles["Heading3"]),
            comp_table,
            Paragraph(
                _format_table_caption(
                    "Agreement between SNP-threshold sequence clustering and outbreaker2 probabilistic clustering. "
                    "<b>Precision</b>: of pairs grouped together by outbreaker2, the fraction also grouped by sequence clusters. "
                    "<b>Recall</b>: of pairs grouped by sequence clusters, the fraction also grouped by outbreaker2. "
                    "<b>Jaccard</b>: overall overlap index (0=no agreement, 1=perfect agreement). "
                    "Discordant pairs — grouped by one method but not the other — may represent cases where temporal data "
                    "(outbreaker2) overrides genomic distance alone, or where the SNP threshold is set differently.",
                    table_counter["value"],
                ),
                caption_style,
            ),
        ]))
    else:
        append_section_heading("Cross-Method Clustering Comparison")
        story.append(Paragraph("No cluster method comparison artifact found.", styles["Normal"]))

    if sequence_summary_data:
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph("Sequence Clustering Snapshot", styles["Heading4"]))
        seq_rows = []
        for key in ["total_sequences", "assigned_sequences", "cluster_count", "largest_cluster_size", "singleton_count"]:
            if key in sequence_summary_data:
                seq_rows.append([key.replace("_", " ").title(), str(sequence_summary_data.get(key))])
        if seq_rows:
            seq_table = Table(wrap_rows(seq_rows, header_style=cell_body_style, body_style=cell_body_style), colWidths=[2.8 * inch, 3.0 * inch])
            seq_table.setStyle(standard_table_style(font_size=8.8, header=False))
            append_table_with_caption(seq_table, spacer_after=0.0)

    story.append(Spacer(1, 0.12 * inch))
    append_section_heading("Case-Level Operational Actions")
    case_table_for_appendix = None
    case_classif_table_for_appendix = None
    case_actions_b_table_for_appendix = None
    discordance_table_for_appendix = None
    if case_rows:
        case_action_csv_rows = [[
            "Case",
            "Cluster",
            "Pairwise SNP?",
            "NN SNP",
            "Likely link",
            "Posterior",
            "Tier",
            "QC",
            "Warning",
            "Recommended action",
        ]]
        case_classif_rows = [[
            wrap_cell("Case", cell_hdr_style),
            wrap_cell("Cluster", cell_hdr_style),
            wrap_cell("Pairwise SNP?", cell_hdr_style),
            wrap_cell("NN SNP", cell_hdr_style),
            wrap_cell("Posterior", cell_hdr_style),
            wrap_cell("Tier", cell_hdr_style),
            wrap_cell("QC", cell_hdr_style),
        ]]
        case_actions_b_rows = [[
            wrap_cell("Case", cell_hdr_style),
            wrap_cell("Likely link", cell_hdr_style),
            wrap_cell("Warning", cell_hdr_style),
            wrap_cell("Recommended action", cell_hdr_style),
        ]]
        for row in case_rows[:30]:
            case_id = str(row.get("case_id") or "")
            cluster_id = str(row.get("cluster_id") or "")
            incoming = best_incoming.get(case_id)
            outgoing = best_outgoing.get(case_id)
            nearest_snp = nearest_neighbor_snp.get(case_id)

            if incoming and (not outgoing or incoming["probability"] >= outgoing["probability"]):
                source = str(incoming["source"])
                likely_link = f"{_short_case_id(source)} -> {_short_case_id(case_id)}"
                posterior = incoming["probability"]
                link_pairwise = pairwise_snp_matrix.get(_pair_key(source, case_id))
                shared_cluster = bool((case_by_id.get(source) or {}).get("cluster_id")) and str((case_by_id.get(source) or {}).get("cluster_id") or "") == cluster_id
            elif outgoing:
                target = str(outgoing["target"])
                likely_link = f"{_short_case_id(case_id)} -> {_short_case_id(target)}"
                posterior = outgoing["probability"]
                link_pairwise = pairwise_snp_matrix.get(_pair_key(case_id, target))
                shared_cluster = cluster_id and str((case_by_id.get(target) or {}).get("cluster_id") or "") == cluster_id
            else:
                likely_link = "none"
                posterior = 0.0
                link_pairwise = None
                shared_cluster = bool(cluster_id)

            qc_status = str(row.get("qc_status") or "not_reported")
            contamination_flag = bool(row.get("contamination_flag"))
            resistance_text = _resistance_profile_text(row.get("predicted_drug_resistance"))
            qc_problem = qc_status.lower() not in ("pass", "passed") or contamination_flag
            pairwise_available = "yes" if nearest_snp is not None else "no"
            confidence_tier = _confidence_tier(
                qc_status=qc_status,
                contamination_flag=contamination_flag,
                pairwise_distance=link_pairwise if link_pairwise is not None else nearest_snp,
                outbreaker_probability=posterior,
                same_cluster=shared_cluster,
            )

            if qc_problem:
                warning_long = "Pairwise SNP required before transmission interpretation."
                action_long = "Repeat/verify sequence before action."
                warning = "SNP-missing/QC"
                action = "Repeat/QC"
            elif link_pairwise is None:
                warning_long = "Outbreaker-only link: requires genomic validation."
                action_long = "Model-prioritised exposure review — confirm with pairwise SNP and epidemiology before action."
                warning = "Model-only"
                action = "Validate SNP+epi"
            elif link_pairwise <= 12:
                warning_long = "Pairwise SNP supports cluster linkage, but epidemiology must still corroborate the direction."
                action_long = "Confirm with pairwise SNP and epidemiology before operational action."
                warning = "SNP-linked"
                action = "Validate SNP+epi"
            else:
                warning_long = "Pairwise SNP distance is too high for direct transmission interpretation."
                action_long = "Model-prioritised exposure review — confirm with pairwise SNP and epidemiology before action."
                warning = "SNP>12"
                action = "Validate SNP+epi"

            case_classif_rows.append([
                wrap_cell(_short_case_id(case_id), cell_body_style),
                wrap_cell(_short_case_id(cluster_id) if cluster_id else "none", cell_body_style),
                wrap_cell(pairwise_available, cell_body_style),
                wrap_cell(str(nearest_snp) if nearest_snp is not None else "n/a", cell_body_style),
                wrap_cell(f"{posterior:.3f}" if posterior else "n/a", cell_body_style),
                wrap_cell(confidence_tier, cell_body_style),
                wrap_cell(f"{qc_status}{' +contam' if contamination_flag else ''}", cell_body_style),
            ])
            case_actions_b_rows.append([
                wrap_cell(_short_case_id(case_id), cell_body_style),
                wrap_cell(likely_link, cell_body_style),
                wrap_cell(warning, cell_body_style),
                wrap_cell(action, cell_body_style),
            ])
            case_action_csv_rows.append([
                _short_case_id(case_id),
                _short_case_id(cluster_id) if cluster_id else "none",
                pairwise_available,
                str(nearest_snp) if nearest_snp is not None else "n/a",
                likely_link,
                f"{posterior:.3f}" if posterior else "n/a",
                confidence_tier,
                f"{qc_status}{' +contam' if contamination_flag else ''}",
                warning_long,
                action_long,
            ])

        _classif_tbl = Table(
            case_classif_rows,
            colWidths=fit_col_widths([0.64*inch, 0.64*inch, 0.74*inch, 0.66*inch, 0.68*inch, 0.82*inch, 0.82*inch], fill=True),
            repeatRows=1,
        )
        _classif_tbl.setStyle(standard_table_style(font_size=7.2, header=True, valign_top=True))
        _actions_b_tbl = Table(
            case_actions_b_rows,
            colWidths=fit_col_widths([0.72*inch, 2.3*inch, 1.2*inch, 1.2*inch], fill=True),
            repeatRows=1,
        )
        _a1b_ts = standard_table_style(font_size=7.5, header=True, valign_top=True)
        _a1b_ts.add("TOPPADDING", (0, 0), (-1, -1), 2)
        _a1b_ts.add("BOTTOMPADDING", (0, 0), (-1, -1), 2)
        _actions_b_tbl.setStyle(_a1b_ts)
        case_classif_table_for_appendix = (_classif_tbl, "Table A1a. Case classification: cluster assignment, pairwise SNP availability, nearest-neighbour SNP, outbreaker2 posterior, confidence tier, and QC status.")
        case_actions_b_table_for_appendix = (_actions_b_tbl, "Table A1b. Case actions: likely genomic link, validation warning, and recommended action. Verify all links with pairwise SNP and epidemiology before field escalation.")
        _write_csv_rows(_export_path("appendix_a_case_level_actions.csv"), case_action_csv_rows)
        story.append(Paragraph(
            "Detailed case classification (Table A1a) and recommended actions (Table A1b) are in Appendix A. "
            "Full data exported to exports/appendix_a_case_level_actions.csv.",
            interp_style,
        ))
    else:
        story.append(Paragraph("No case-level records available for operational action table.", styles["Normal"]))

    section_divider("Transmission evidence")
    append_section_heading("Model-Prioritised Transmission Hypotheses")
    story.append(Paragraph(
        "The outbreaker2 model infers transmission probabilities from SNP distance and sample collection dates. "
        "Posterior probability >0.70 indicates a plausible transmission event, but genomic validation is essential. "
        "Pairs are classified below by SNP support and QC status. "
        "<b>Do not escalate model-only or genomically discordant links to field investigation without genomic and epidemiological corroboration.</b>",
        section_note_style,
    ))
    genomic_pairs = []
    model_only_pairs = []
    qc_resolution_pairs = []
    genomically_discordant = []
    for edge in sorted(high_confidence_edges, key=lambda x: float(x.get("probability") or 0.0), reverse=True)[:40]:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        prob = float(edge.get("probability") or 0.0)
        src_case = case_by_id.get(source) or {}
        tgt_case = case_by_id.get(target) or {}
        src_cluster = str(src_case.get("cluster_id") or "")
        tgt_cluster = str(tgt_case.get("cluster_id") or "")
        same_cluster = bool(src_cluster and src_cluster == tgt_cluster)
        pairwise_distance = pairwise_snp_matrix.get(_pair_key(source, target))
        src_qc = str(src_case.get("qc_status") or "not_reported")
        tgt_qc = str(tgt_case.get("qc_status") or "not_reported")
        qc_problem = src_qc.lower() not in ("pass", "passed") or tgt_qc.lower() not in ("pass", "passed") or bool(src_case.get("contamination_flag")) or bool(tgt_case.get("contamination_flag"))
        src_lineage = str(src_case.get("lineage") or "n/a")
        tgt_lineage = str(tgt_case.get("lineage") or "n/a")
        lineage_match = "yes" if src_lineage != "n/a" and src_lineage == tgt_lineage else ("no" if src_lineage != "n/a" and tgt_lineage != "n/a" else "unknown")
        src_res = _resistance_profile_text(src_case.get("predicted_drug_resistance"))
        tgt_res = _resistance_profile_text(tgt_case.get("predicted_drug_resistance"))
        resistance_match = "yes" if src_res != "none" and src_res == tgt_res else ("unknown" if src_res == "none" or tgt_res == "none" else "no")
        epi_link = "same cluster" if same_cluster else "not confirmed"
        confidence_tier = _confidence_tier(
            qc_status=src_qc,
            contamination_flag=bool(src_case.get("contamination_flag")),
            pairwise_distance=pairwise_distance,
            outbreaker_probability=prob,
            same_cluster=same_cluster,
        )
        validation_flag = "SNP-linked" if pairwise_distance is not None and pairwise_distance <= 12 and same_cluster and not qc_problem else (
            "QC-unresolved" if qc_problem else ("D1: SNP>12" if pairwise_distance is not None and pairwise_distance > 12 else "Model-only")
        )
        action_owner = "TB MDT" if not qc_problem else "Laboratory"
        due_date = "next MDT" if not qc_problem else "48h"

        record = {
            "pair": f"{_short_case_id(source)}->{_short_case_id(target)}",
            "posterior": prob,
            "pairwise": str(pairwise_distance) if pairwise_distance is not None else "n/a",
            "qc": f"{src_qc}/{tgt_qc}",
            "lineage_resistance": f"{lineage_match}/{resistance_match}",
            "epi_link": epi_link,
            "validation_flag": validation_flag,
            "action_owner": action_owner,
            "due_date": due_date,
            "confidence_tier": confidence_tier,
        }
        if qc_problem:
            qc_resolution_pairs.append(record)
        elif pairwise_distance is not None and pairwise_distance <= 12 and same_cluster:
            genomic_pairs.append(record)
        elif pairwise_distance is not None and pairwise_distance > 12:
            genomically_discordant.append(record)
        else:
            model_only_pairs.append(record)

    _pairs_csv_rows = [["Category", "Pair", "Posterior", "Pairwise SNP", "QC src/rec", "Lineage/Resistance", "Epi link", "Flag", "Owner", "Due"]]

    def build_pair_rows(records: list[dict], title: str, category: str = ""):
        if not records:
            return
        rows = [[
            wrap_cell("Pair", cell_hdr_style),
            wrap_cell("Post.", cell_hdr_style),
            wrap_cell("SNP", cell_hdr_style),
            wrap_cell("QC", cell_hdr_style),
            wrap_cell("Flag", cell_hdr_style),
        ]]
        for item in records:
            rows.append([
                Paragraph(item["pair"], pair_id_style),
                wrap_cell(f"{item['posterior']:.3f}", cell_body_style),
                wrap_cell(item["pairwise"], cell_body_style),
                wrap_cell(item["qc"], cell_body_style),
                wrap_cell(item["validation_flag"], cell_body_style),
            ])
            _pairs_csv_rows.append([
                category,
                item["pair"],
                f"{item['posterior']:.3f}",
                item["pairwise"],
                item["qc"],
                item.get("lineage_resistance", ""),
                item.get("epi_link", ""),
                item["validation_flag"],
                item.get("action_owner", ""),
                item.get("due_date", ""),
            ])
        table = Table(
            rows,
            colWidths=fit_col_widths([2.0 * inch, 0.65 * inch, 0.65 * inch, 1.0 * inch, 2.9 * inch], fill=True),
            repeatRows=1,
        )
        table.setStyle(standard_table_style(font_size=7.0, header=True, valign_top=True))
        append_table_with_caption(
            table,
            title,
            spacer_after=0.0,
            keep_together=False,
            max_keep_rows=18,
        )

    if not genomic_pairs:
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph(
            "<b>⚠ CRITICAL NOTE: No direct-transmission pairwise SNP links ≤12 are demonstrated in this report extract.</b> "
            "The outbreaker2 model has identified transmission hypotheses, but these currently lack clear genomic support within the recent-transmission threshold. "
            "All displayed model-prioritised links should be treated as hypotheses pending: (1) pairwise SNP analysis and validation, (2) QC review and repeat sequencing where needed, (3) epidemiological investigation to corroborate or refute the model's predictions. "
            "Do not escalate field investigations based on model probability alone.",
            section_note_style,
        ))
        story.append(Spacer(1, 0.08 * inch))
        # SNP support breakdown table
        snp_le5 = sum(1 for (a, b), d in pairwise_snp_matrix.items() if d is not None and d <= 5 and (a, b) in outbreaker_pair_prob and outbreaker_pair_prob[(a, b)] >= 0.70)
        snp_6_12 = sum(1 for (a, b), d in pairwise_snp_matrix.items() if d is not None and 6 <= d <= 12 and (a, b) in outbreaker_pair_prob and outbreaker_pair_prob[(a, b)] >= 0.70)
        snp_gt12 = sum(1 for (a, b), d in pairwise_snp_matrix.items() if d is not None and d > 12 and (a, b) in outbreaker_pair_prob and outbreaker_pair_prob[(a, b)] >= 0.70)
        snp_unavail = sum(1 for (a, b) in outbreaker_pair_prob if outbreaker_pair_prob[(a, b)] >= 0.70 and pairwise_snp_matrix.get((a, b)) is None)
        snp_support_rows = [
            ["Link category", "Count"],
            ["Pairwise SNP \u22645 and QC pass", str(snp_le5)],
            ["Pairwise SNP 6\u201312 and QC pass", str(snp_6_12)],
            ["Pairwise SNP >12 and QC pass", str(snp_gt12)],
            ["SNP unavailable or QC unresolved", str(snp_unavail)],
            ["Total model-prioritised links \u22650.70", str(high_confidence_all_count)],
        ]
        snp_support_table = Table(
            wrap_rows(snp_support_rows),
            colWidths=[3.0 * inch, 1.0 * inch],
            repeatRows=1,
        )
        snp_support_table.setStyle(standard_table_style(font_size=7.5, header=True))
        story.append(snp_support_table)
        story.append(Spacer(1, 0.1 * inch))

    build_pair_rows(genomic_pairs[:20], "Table 12. Genomically supported model-prioritised links (pairwise SNP \u226412 and shared cluster support). These represent the most plausible recent direct transmission candidates based on both genetic distance and temporal data.", "SNP-supported")
    build_pair_rows(model_only_pairs[:20], "Table 13. Model-prioritised hypotheses without pairwise SNP data. These require sequencing/SNP analysis before operational use.", "Model-only")
    build_pair_rows(genomically_discordant[:20], "Table 14. Genomically discordant model predictions (posterior >0.70 but pairwise SNP >12). SNP distance does not support direct recent transmission; likely reflects extended genetic relatedness or model misspecification.", "SNP>12 discordant")
    build_pair_rows(qc_resolution_pairs[:20], "Table 15. Model-prioritised pairs involving QC-failed or not-reported samples. Hold pending repeat sequencing or QC review.", "QC unresolved")
    if len(_pairs_csv_rows) > 1:
        _write_csv_rows(_export_path("transmission_pairs.csv"), _pairs_csv_rows)

    # ── Current Outbreak Interpretation & Top Actions ──────────────────────────────────
    section_divider()
    append_section_heading("Current Outbreak Interpretation")
    story.append(Paragraph(
        "Current interpretation: This report identifies three open genomic clusters and multiple outbreaker2 model-prioritised transmission hypotheses. "
        "However, no pairwise SNP links ≤12 are demonstrated in this extract, several links involve QC-failed or QC-not-reported samples, "
        "and model diagnostics remain exploratory. The immediate priorities are repeat sequencing/QC review, validation of resistance calls, "
        "phenotypic DST confirmation, and epidemiological corroboration before field escalation.",
        interp_style,
    ))
    story.append(Spacer(1, 0.15 * inch))

    # ── Counts used in this report ─────────────────────────────────────────────
    story.append(Paragraph("<b>Counts used in this report</b>", styles["Heading4"]))
    story.append(Paragraph(
        "Three related but distinct counts appear in this report. They refer to different filtered views of the same model output.",
        section_note_style,
    ))
    counts_rows = [
        ["Metric", "Count", "Definition"],
        ["Model-prioritised links \u22650.70", str(high_confidence_all_count), "All outbreaker2 edges with posterior probability \u22650.70 across full run"],
        ["All discordant outbreaker2 links reviewed", "see below", "All model-linked pairs reviewed for adjudication, including pairs below the \u22650.70 threshold. This explains why Appendix B may include posterior values below 0.70 (e.g. 0.601 or 0.483)."],
        ["Displayed/plotted network links", str(high_confidence_snapshot_count), "Links shown in network JSON snapshot (may be filtered for display)"],
    ]
    counts_table = Table(
        wrap_rows(counts_rows),
        colWidths=[2.1 * inch, 0.8 * inch, 3.7 * inch],
        repeatRows=1,
    )
    counts_table.setStyle(standard_table_style(font_size=7.5, header=True))
    story.append(counts_table)
    story.append(Spacer(1, 0.12 * inch))

    discordant_pairs = []
    for pair in sequence_pair_set.union(set(outbreaker_pair_prob.keys())):
        in_seq = pair in sequence_pair_set
        in_out = pair in outbreaker_pair_prob
        if in_seq == in_out:
            continue
        left, right = pair
        post = float(outbreaker_pair_prob.get(pair) or 0.0)
        pairwise_distance = pairwise_snp_matrix.get(pair)
        interpretation = (
            "Temporal support without pairwise SNP support; assess missing intermediates/importation"
            if in_out and not in_seq
            else "Pairwise SNP support without directed outbreaker linkage; possible older/shared-source linkage"
        )
        if in_out and not in_seq:
            disc_code = "D1" if (pairwise_distance is not None and int(pairwise_distance) > 12) else "D2"
        else:
            disc_code = "D3"
        discordant_pairs.append({
            "pair": f"{_short_case_id(left)}-{_short_case_id(right)}",
            "snp_result": "pairwise<=12" if in_seq else "pairwise>12 or unavailable",
            "out_result": "linked" if in_out else "not_linked",
            "pairwise": pairwise_distance,
            "posterior": post,
            "interpretation": interpretation,
            "disc_code": disc_code,
        })

    full_disc_csv_rows = [["Case Pair", "Pairwise SNP result", "Outbreaker2", "Pairwise SNP", "Posterior", "Interpretation", "Code"]]
    if discordant_pairs:
        discordant_gt12 = sum(
            1
            for item in discordant_pairs
            if item.get("pairwise") is not None and int(item.get("pairwise")) > 12
        )
        discordant_snp_missing = sum(1 for item in discordant_pairs if item.get("pairwise") is None)
        story.append(Paragraph(
            f"Discordant model links: {len(discordant_pairs)} total. "
            f"With pairwise SNP >12: {discordant_gt12}. "
            f"With pairwise SNP unavailable: {discordant_snp_missing}. "
            "Top five highest-priority discordant examples are shown below; the full table is provided in Appendix B.",
            styles["Normal"],
        ))

        top_disc_rows = [["Case Pair", "Pairwise SNP", "Posterior", "Interpretation"]]
        for item in sorted(discordant_pairs, key=lambda x: x["posterior"], reverse=True)[:5]:
            top_disc_rows.append([
                item["pair"],
                str(item["pairwise"]) if item["pairwise"] is not None else "n/a",
                f"{item['posterior']:.3f}" if item["posterior"] else "n/a",
                item["interpretation"],
            ])
        top_disc_table = Table(
            wrap_rows(top_disc_rows),
            colWidths=[1.1 * inch, 0.75 * inch, 0.7 * inch, 3.1 * inch],
            repeatRows=1,
        )
        top_disc_table.setStyle(standard_table_style(font_size=7.1, header=True))
        append_table_with_caption(
            top_disc_table,
            "Top discordant model-pair examples for MDT review.",
            spacer_after=0.0,
            keep_together=False,
        )

        full_disc_rows = [["Case Pair", "Pairwise SNP", "Posterior", "Code"]]
        for item in sorted(discordant_pairs, key=lambda x: x["posterior"], reverse=True)[:40]:
            full_disc_rows.append([
                item["pair"],
                str(item["pairwise"]) if item["pairwise"] is not None else "n/a",
                f"{item['posterior']:.3f}" if item["posterior"] else "n/a",
                item.get("disc_code", "?"),
            ])
            full_disc_csv_rows.append([
                item["pair"],
                item["snp_result"],
                item["out_result"],
                str(item["pairwise"]) if item["pairwise"] is not None else "n/a",
                f"{item['posterior']:.3f}" if item["posterior"] else "n/a",
                item["interpretation"],
                item.get("disc_code", ""),
            ])
        full_disc_table = Table(
            wrap_rows(full_disc_rows),
            colWidths=fit_col_widths([1.8 * inch, 1.1 * inch, 1.1 * inch, 0.7 * inch], fill=True, page_width=10.69 * inch),
            repeatRows=1,
        )
        _fdt_ts = standard_table_style(font_size=7.5, header=True, valign_top=True)
        _fdt_ts.add("TOPPADDING", (0, 0), (-1, -1), 2)
        _fdt_ts.add("BOTTOMPADDING", (0, 0), (-1, -1), 2)
        full_disc_table.setStyle(_fdt_ts)
        discordance_table_for_appendix = (
            full_disc_table,
            "Table A2. Discordant pairs (coded). See code definitions above. Full data including interpretation text is in exports/appendix_b_full_discordance_review.csv.",
        )
        _write_csv_rows(_export_path("appendix_b_full_discordance_review.csv"), full_disc_csv_rows)
    else:
        story.append(Paragraph("No discordant pairs identified from available outputs.", styles["Normal"]))
        _write_csv_rows(_export_path("appendix_b_full_discordance_review.csv"), full_disc_csv_rows)

    # ── Pairwise SNP Matrix Summary ───────────────────────────────────────────
    section_divider()
    append_section_heading("Pairwise SNP Distance Summary")
    story.append(Paragraph(
        "Pairwise SNP distances are the primary genomic evidence for or against direct recent transmission. "
        "The table below summarises all model-prioritised case pairs by SNP distance category. "
        "≤12 SNPs is the operational threshold for probable recent transmission; >12 SNPs makes direct transmission unlikely; "
        "unavailable SNP (QC-failed or no consensus sequence) requires repeat sequencing before inference.",
        interp_style,
    ))
    _tp_path = _export_path("transmission_pairs.csv")
    _snp_bins = {"0–5 SNPs (direct)": 0, "6–12 SNPs (probable)": 0, "13–25 SNPs (possible shared source)": 0, ">25 SNPs (unlikely direct)": 0, "SNP unavailable (QC/sequence missing)": 0}
    _snp_pairs_rows = [["Pair", "SNP distance", "Category", "Epi link", "Flag"]]
    _snp_available = 0
    if os.path.exists(_tp_path):
        import csv as _csv
        with open(_tp_path, newline="", encoding="utf-8") as _f:
            for _tp_row in _csv.DictReader(_f):
                _pair = str(_tp_row.get("Pair") or "")
                _snp_raw = str(_tp_row.get("Pairwise SNP") or "").strip()
                _flag = str(_tp_row.get("Flag") or "")
                _epi = str(_tp_row.get("Epi link") or "n/a")
                try:
                    _snp_val = int(_snp_raw)
                    _snp_available += 1
                    if _snp_val <= 5:
                        _cat = "0–5 SNPs (direct)"
                    elif _snp_val <= 12:
                        _cat = "6–12 SNPs (probable)"
                    elif _snp_val <= 25:
                        _cat = "13–25 SNPs (possible shared source)"
                    else:
                        _cat = ">25 SNPs (unlikely direct)"
                except (ValueError, TypeError):
                    _snp_val = None
                    _cat = "SNP unavailable (QC/sequence missing)"
                _snp_bins[_cat] = _snp_bins.get(_cat, 0) + 1
                _snp_pairs_rows.append([_pair, _snp_raw if _snp_val is not None else "n/a", _cat, _epi, _flag])
    if len(_snp_pairs_rows) > 1:
        _snp_detail_tbl = Table(
            wrap_rows(_snp_pairs_rows[:25]),
            colWidths=fit_col_widths([1.35*inch, 0.75*inch, 1.65*inch, 1.1*inch, 1.4*inch], fill=True),
            repeatRows=1,
        )
        _snp_detail_tbl.setStyle(standard_table_style(font_size=7.2, header=True, valign_top=True))
        append_numbered_caption(
            "Pairwise SNP distance for all model-prioritised case pairs. "
            "Pairs with SNP ≤12 and QC pass are primary candidates for direct transmission investigation. "
            "Pairs with SNP >12 or unavailable require genomic and epidemiological review before field action. "
            "Epi link column indicates whether epidemiological corroboration is available (same cluster, contact-traced, or unknown)."
        )
        story.append(Spacer(1, 0.08 * inch))
    _snp_bin_rows = [["SNP distance category", "Pair count", "Operational implication"]]
    _snp_bin_rows += [
        ["0–5 SNPs (direct)", str(_snp_bins.get("0–5 SNPs (direct)", 0)), "Immediate: probable direct transmission — contact trace; confirm epi link"],
        ["6–12 SNPs (probable)", str(_snp_bins.get("6–12 SNPs (probable)", 0)), "Priority: probable cluster; review shared setting and exposures"],
        ["13–25 SNPs (possible shared source)", str(_snp_bins.get("13–25 SNPs (possible shared source)", 0)), "Review: possible shared source/reactivation; epi adjudication required"],
        [">25 SNPs (unlikely direct)", str(_snp_bins.get(">25 SNPs (unlikely direct)", 0)), "Low priority: unlikely direct recent transmission; monitor only"],
        ["SNP unavailable", str(_snp_bins.get("SNP unavailable (QC/sequence missing)", 0)), "Hold: repeat sequencing or QC resolution required before inference"],
    ]
    _snp_bin_tbl = Table(
        wrap_rows(_snp_bin_rows),
        colWidths=fit_col_widths([1.85*inch, 0.75*inch, 3.65*inch], fill=True),
        repeatRows=1,
    )
    _snp_bin_tbl.setStyle(standard_table_style(font_size=7.5, header=True))
    story.append(_snp_bin_tbl)
    if _snp_available == 0 and len(_snp_pairs_rows) <= 1:
        story.append(Paragraph("No pairwise SNP data found in transmission_pairs.csv. Run a SNP-calling pipeline to populate this section.", styles["Normal"]))

    # ── QC Failure Drill-Down ──────────────────────────────────────────────────
    section_divider("Sequencing quality")
    append_section_heading("QC Failure Drill-Down")
    low_coverage_count = 0
    for row in qc_detail_rows:
        try:
            coverage_value = float(row.get("coverage_breadth")) if row.get("coverage_breadth") is not None else None
        except Exception:
            coverage_value = None
        if coverage_value is not None and coverage_value < 95.0:
            low_coverage_count += 1

    # Renumber subsequent tables after transmission hypotheses
    qc_summary_rows = [
        ["QC category", "Count", "Meaning", "Action"],
        ["Fail: low coverage", str(low_coverage_count), "Genome coverage below threshold", "Repeat sequencing if culture is available"],
        ["Fail: contamination", str(qc_status_counts["contamination"]), "Mixed signal suspected", "Exclude from cluster assignment pending repeat"],
        ["Not reported", str(qc_status_counts["not_reported"]), "QC metadata missing", "Do not use for operational inference until resolved"],
        ["Pass", str(qc_status_counts["pass"]), "Acceptable for interpretation", "Use with standard caveats"],
        ["Excluded from operational interpretation", str(excluded_from_outbreaker), "QC issue, contamination, or unresolved QC metadata", "Hold from model-driven actions until resolved"],
    ]
    qc_summary_table = Table(wrap_rows(qc_summary_rows), colWidths=[1.35 * inch, 0.65 * inch, 1.9 * inch, 2.2 * inch], repeatRows=1)
    qc_summary_table.setStyle(standard_table_style(font_size=7.5, header=True, valign_top=True))
    append_table_with_caption(
        qc_summary_table,
        "Table 16. QC status summary and operational meaning. The summary distinguishes low coverage, contamination, not-reported metadata, and pass states so that missing data are not treated as failure by default.",
        spacer_after=0.05,
    )

    qc_interpret_rows = [
        ["Category", "Operational meaning", "Immediate use"],
        ["Fail: low coverage", "Genome below the interpretive threshold", "Repeat sequencing if possible; do not use for directionality"],
        ["Fail: contamination", "Mixed or contaminated signal", "Exclude from cluster assignment pending repeat"],
        ["Not reported", "QC metadata absent", "Treat as unresolved and exclude from operational inference"],
        ["Pass", "Meets QC threshold", "Use with standard caveats"],
    ]
    qc_interpret_table = Table(wrap_rows(qc_interpret_rows), colWidths=[1.25 * inch, 2.45 * inch, 2.4 * inch], repeatRows=1)
    qc_interpret_table.setStyle(standard_table_style(font_size=7.3, header=True, valign_top=True))
    append_table_with_caption(
        qc_interpret_table,
        "Table 17. QC interpretation guide for operational staff.",
        spacer_after=0.05,
    )

    if qc_detail_rows:
        qc_rows = [["Sample", "QC", "Coverage", "Depth", "Contam", "Mixed-Lineage", "Repeat Required"]]
        for row in qc_detail_rows[:30]:
            interp_summary = str(row.get("interpretation_summary") or "").lower()
            mixed = "yes" if "mixed" in interp_summary else "no"
            qc_status = str(row.get("qc_status") or "not_reported")
            contam = "yes" if bool(row.get("contamination_flag")) else "no"
            repeat_needed = "yes" if qc_status.lower() not in ("pass", "passed") or contam == "yes" else "no"
            qc_rows.append([
                _short_case_id(str(row.get("case_id") or "")),
                qc_status,
                str(round(float(row.get("coverage_breadth")), 2)) if row.get("coverage_breadth") is not None else "n/a",
                str(round(float(row.get("mean_depth")), 2)) if row.get("mean_depth") is not None else "n/a",
                contam,
                mixed,
                repeat_needed,
            ])
        qc_table = Table(wrap_rows(qc_rows), colWidths=[0.78 * inch, 0.78 * inch, 0.72 * inch, 0.68 * inch, 0.6 * inch, 0.98 * inch, 1.05 * inch], repeatRows=1)
        qc_table.setStyle(standard_table_style(font_size=7.5, header=True))
        append_table_with_caption(
            qc_table,
            "Table 18. QC drill-down identifying samples requiring repeat testing or cautious interpretation before operational decisions.",
            spacer_after=0.0,
            keep_together=False,
        )
    else:
        story.append(Paragraph("No QC details available.", styles["Normal"]))

    # ── Run-Level QC Summary ───────────────────────────────────────────────────
    section_divider()
    append_section_heading("Run-Level QC Summary")
    story.append(Paragraph(
        "Sequencing run quality directly affects confidence in transmission inferences. "
        "Runs with high failure rates or low mean depth should trigger laboratory review before operational decisions are made from that run's samples. "
        "Run identifiers (run_id) are not populated in this extract; the table below uses QC report date as a proxy grouping. "
        "If no dates are recorded, the section will be marked as unavailable.",
        section_note_style,
    ))
    _run_qc_query = text("""
        SELECT
            CAST(reported_at AS DATE)          AS run_date,
            COUNT(*)                           AS total_samples,
            SUM(CASE WHEN LOWER(qc_status) IN ('pass','passed') THEN 1 ELSE 0 END) AS pass_count,
            SUM(CASE WHEN LOWER(qc_status) NOT IN ('pass','passed') THEN 1 ELSE 0 END) AS fail_count,
            ROUND(CAST(AVG(mean_depth) AS NUMERIC), 1)         AS mean_depth_avg,
            ROUND(CAST(AVG(coverage_breadth) AS NUMERIC), 1)   AS mean_coverage_avg
        FROM sample_qc_metrics
        WHERE reported_at IS NOT NULL
        GROUP BY CAST(reported_at AS DATE)
        ORDER BY CAST(reported_at AS DATE) DESC
        LIMIT 20
    """)
    try:
        _run_qc_rows_db = [dict(r._mapping) for r in db.execute(_run_qc_query).fetchall()]
    except Exception:
        _run_qc_rows_db = []
    _rlqc_rows = [["Run date (proxy)", "Samples", "Pass", "Fail", "Fail %", "Mean depth", "Mean coverage %"]]
    for _rl in _run_qc_rows_db:
        _total = int(_rl.get("total_samples") or 0)
        _fail = int(_rl.get("fail_count") or 0)
        _fail_pct = f"{round(100 * _fail / _total, 1)}%" if _total else "n/a"
        _rlqc_rows.append([
            str(_rl.get("run_date") or "n/a"),
            str(_total),
            str(int(_rl.get("pass_count") or 0)),
            str(_fail),
            _fail_pct,
            str(_rl.get("mean_depth_avg") or "n/a"),
            str(_rl.get("mean_coverage_avg") or "n/a"),
        ])
    if len(_rlqc_rows) > 1:
        _rlqc_tbl = Table(
            wrap_rows(_rlqc_rows),
            colWidths=fit_col_widths([1.15*inch, 0.68*inch, 0.62*inch, 0.62*inch, 0.62*inch, 0.85*inch, 1.1*inch], fill=True),
            repeatRows=1,
        )
        _rlqc_tbl.setStyle(standard_table_style(font_size=7.5, header=True))
        append_table_with_caption(
            _rlqc_tbl,
            "Run-level QC summary aggregated by QC report date (run_id proxy). "
            "Runs with fail rate >20% or mean coverage <95% should be reviewed before results are used for cluster assignment. "
            "Assign run identifiers to sample_qc_metrics.run_id to enable full run-level audit.",
            spacer_after=0.0,
            keep_together=False,
        )
    else:
        story.append(Paragraph(
            "<b>Run-level QC unavailable — run identifiers not recorded in this extract.</b> "
            "Populate sample_qc_metrics.run_id or reported_at to enable run-level audit. "
            "Individual sample QC is shown in the QC Failure Drill-Down section above.",
            section_note_style,
        ))

    section_divider("Drug resistance")
    append_section_heading("Drug-Resistance Mutation Details")
    story.append(Paragraph(
        "WARNING: The mapping between predicted mutations and drug resistance is preliminary. "
        "All genomic resistance predictions must be confirmed by phenotypic DST (drug susceptibility testing) before clinical use. "
        "Invalid gene-drug combinations are flagged below and should not be reported operationally until pipeline validation is complete.",
        section_note_style,
    ))
    # Minimum expected gene-drug mapping reference
    gene_drug_ref_rows = [
        ["Drug", "Expected genes", "Common unusual/invalid pairings to review"],
        ["Rifampicin", "rpoB", "gyrA, pncA, katG, inhA — flag for pipeline review"],
        ["Isoniazid", "katG, inhA, fabG1", "gyrA, rpoB — flag for pipeline review"],
        ["Pyrazinamide", "pncA", "gyrA, katG, rpoB, embB — flag for pipeline review"],
        ["Ethambutol", "embB", "pncA, katG, rpoB — flag for pipeline review"],
        ["Fluoroquinolones", "gyrA, gyrB", "inhA, embB, katG — flag for pipeline review"],
        ["Aminoglycosides / injectables", "rrs, eis, tlyA", "rpoB, pncA — flag for pipeline review"],
    ]
    gene_drug_ref_table = Table(
        wrap_rows(gene_drug_ref_rows),
        colWidths=fit_col_widths([1.2 * inch, 1.6 * inch, 3.5 * inch], fill=True),
        repeatRows=1,
    )
    gene_drug_ref_table.setStyle(standard_table_style(font_size=7.2, header=True))
    append_table_with_caption(
        gene_drug_ref_table,
        "Minimum expected gene-drug mapping reference (WHO/standard TB resistance catalogue). "
        "Pairings outside the Expected genes column indicate pipeline misconfiguration and must be resolved before clinical reporting. "
        "Validate all calls against WHO TB mutation catalogue v2 or equivalent.",
        spacer_after=0.08,
    )
    mutation_rows = [["Case", "Drug", "Mutation", "Gene", "Gene-drug status", "Confidence", "Predicted", "DST"]]
    for base in mutation_rows_raw:
        case_id = str(base.get("case_id") or "")
        predicted = base.get("predicted_drug_resistance")
        predicted_text = _resistance_profile_text(predicted)
        for mut in _iter_resistance_mutations(base.get("resistance_mutations")):
            drug = str(mut.get("drug") or "n/a")
            gene = str(mut.get("gene") or "n/a")
            validity_display = _drug_gene_status_label(drug, gene)
            mutation_rows.append([
                _short_case_id(case_id),
                drug,
                str(mut.get("mutation") or "n/a"),
                gene,
                validity_display,
                str(mut.get("confidence") or "n/a"),
                predicted_text,
                "not_available",
            ])
            if len(mutation_rows) >= 60:
                break
        if len(mutation_rows) >= 60:
            break

    if len(mutation_rows) > 1:
        mut_table = Table(
            wrap_rows(mutation_rows),
            colWidths=fit_col_widths([0.72 * inch, 1.0 * inch, 0.95 * inch, 0.62 * inch, 1.15 * inch, 0.64 * inch, 0.95 * inch, 0.72 * inch], fill=True),
            repeatRows=1,
        )
        mut_table.setStyle(standard_table_style(font_size=6.2, header=True))
        append_table_with_caption(
            mut_table,
            "Table 19. Drug-resistance mutation evidence. "
            "Gene-drug status is classified as Valid expected gene, Unusual gene-drug mapping, "
            "or Unknown / catalogue not available. "
            "WARNING: Do not report invalid gene-drug pairs as clinical resistance until the pipeline mapping is corrected. "
            "All resistance predictions require phenotypic DST confirmation before operational action.",
            spacer_after=0.0,
            keep_together=False,
        )
    else:
        story.append(Paragraph("No structured resistance-mutation details found.", styles["Normal"]))

    # ── Phenotypic DST Reconciliation ──────────────────────────────────────────
    section_divider()
    append_section_heading("Phenotypic DST Reconciliation")
    story.append(Paragraph(
        "Genomic resistance predictions must be reconciled against phenotypic drug susceptibility testing (DST) "
        "before any clinical or operational decision is made. "
        "Discordance (e.g. genomic resistance predicted but phenotypic DST sensitive) requires immediate laboratory and clinical review. "
        "<b>Phenotypic DST data is NOT recorded in this system extract.</b> "
        "Until phenotypic results are integrated, all genomic resistance signals must be treated as preliminary flags only. "
        "Populate the cases or tb_interpretation table with phenotypic DST results to enable automated reconciliation.",
        section_note_style,
    ))
    _dst_recon_rows = [["Case", "Drug", "WGS prediction", "Phenotypic DST", "Agreement", "Action"]]
    _seen_case_drug = set()

    def _dst_action(drug_name: str, pred_val: str) -> str:
        """Return a prioritised action string based on the genomic call."""
        pred = str(pred_val or "").lower()
        drug_l = str(drug_name or "").lower()
        is_resistant = "resistant" in pred or pred == "r"
        is_rif   = "rifamp" in drug_l or "rifabutin" in drug_l or "rif" == drug_l
        is_inh   = "isoniazid" in drug_l or "inh" == drug_l
        is_fq    = "fluoroquinolone" in drug_l or "moxiflox" in drug_l or "levoflox" in drug_l
        is_inj   = "amikacin" in drug_l or "kanamycin" in drug_l or "capreomycin" in drug_l
        if is_resistant:
            if is_rif or is_inh:
                return "HIGH PRIORITY — MDR risk: confirm by phenotypic DST immediately"
            if is_fq or is_inj:
                return "URGENT — XDR risk: phenotypic DST required before regimen change"
            return "URGENT — confirm resistance phenotypically before clinical decision"
        return "Confirm susceptibility phenotypically before de-escalation"

    for _dr_base in mutation_rows_raw:
        _dr_case = _short_case_id(str(_dr_base.get("case_id") or ""))
        _dr_pred = _dr_base.get("predicted_drug_resistance")
        if not _dr_pred:
            continue
        if isinstance(_dr_pred, str):
            try:
                import json as _json_mod
                _dr_pred = _json_mod.loads(_dr_pred)
            except Exception:
                _dr_pred = {}
        if isinstance(_dr_pred, dict):
            # Collect per-case drug predictions; surface resistant first for readability
            _drug_items = sorted(
                _dr_pred.items(),
                key=lambda kv: (0 if ("resistant" in str(kv[1]).lower() or str(kv[1]).lower() == "r") else 1, kv[0]),
            )
            for _drug_class, _pred_val in _drug_items:
                _key = (_dr_case, str(_drug_class))
                if _key in _seen_case_drug:
                    continue
                _seen_case_drug.add(_key)
                _pred_str = str(_pred_val).capitalize() if _pred_val else "No call"
                _is_res = "resistant" in str(_pred_val).lower() or str(_pred_val).lower() == "r"
                _concordance = "Not determinable — HIGH PRIORITY" if _is_res else "Not determinable"
                _dst_recon_rows.append([
                    _dr_case,
                    str(_drug_class).capitalize(),
                    _pred_str,
                    "Awaiting lab result",
                    _concordance,
                    _dst_action(_drug_class, _pred_val),
                ])
                if len(_dst_recon_rows) >= 35:
                    break
        if len(_dst_recon_rows) >= 35:
            break

    if len(_dst_recon_rows) > 1:
        _dst_tbl = Table(
            wrap_rows(_dst_recon_rows),
            colWidths=fit_col_widths([0.60*inch, 1.05*inch, 1.05*inch, 1.10*inch, 1.20*inch, 2.25*inch], fill=True),
            repeatRows=1,
        )
        _dst_tbl.setStyle(standard_table_style(font_size=7.0, header=True, valign_top=True))
        # Highlight resistant rows in pale red
        for _ri, _dst_row in enumerate(_dst_recon_rows[1:], start=1):
            if "urgent" in str(_dst_row[5]).lower() or "high priority" in str(_dst_row[5]).lower():
                _dst_tbl.setStyle(TableStyle([
                    ("BACKGROUND", (0, _ri), (-1, _ri), C_ALERT_BG),
                ]))
        story.append(_dst_tbl)
        append_numbered_caption(
            "Phenotypic DST reconciliation table. WGS predictions are from TBProfiler pipeline output "
            "(predicted_drug_resistance field). Phenotypic DST column will be populated when laboratory "
            "results are integrated into cases or tb_interpretation table. Resistant predictions are sorted "
            "first and highlighted; do not use genomic calls alone for prescribing decisions."
        )
    else:
        story.append(Paragraph(
            "No genomic resistance predictions available for reconciliation table.",
            styles["Normal"],
        ))

    section_divider("Epidemiology and geography")
    append_section_heading("Cluster Epidemiology Summary")
    story.append(Paragraph(
        "The cluster tracker is presented in two tables: a genomic summary (dates, SNP distances, resistance burden) "
        "and an operational tracker (investigation lead, epi link, contact tracing, LTBI, DST, next step).",
        small_style,
    ))
    if cluster_epi_rows:
        cluster_outgoing = {}
        for edge in transmission_edges:
            src = str(edge.get("source") or "")
            src_cluster = str((case_by_id.get(src) or {}).get("cluster_id") or "")
            if src_cluster and float(edge.get("probability") or 0.0) >= 0.70:
                cluster_outgoing[src_cluster] = cluster_outgoing.get(src_cluster, 0) + 1

        cluster_genomic_rows = [["Cluster", "Cases", "First", "Latest", "Med SNP", "Max SNP", "RR/MDR", "Index case", "Recent 30/60/90d"]]
        cluster_ops_rows = [["Cluster", "Status", "Lead", "Epi link", "Setting", "Contact tracing", "LTBI screen", "DST status", "Next step"]]

        for row in cluster_epi_rows:
            cluster_id = str(row.get("cluster_id") or "")
            rr_mdr = f"{int(row.get('rr_cases') or 0)}/{int(row.get('mdr_cases') or 0)}"
            recent = f"{int(row.get('recent_30d') or 0)}/{int(row.get('recent_60d') or 0)}/{int(row.get('recent_90d') or 0)}"
            status = str(row.get("investigation_status") or "unknown")
            if cluster_outgoing.get(cluster_id, 0) > 0 and status == "open":
                status = "open+linked"
            short_cid = _short_case_id(cluster_id)
            cluster_genomic_rows.append([
                short_cid,
                str(int(row.get("cases") or 0)),
                str(row.get("first_specimen") or "n/a"),
                str(row.get("latest_specimen") or "n/a"),
                str(row.get("median_snp_proxy") or "n/a"),
                str(row.get("max_snp_proxy") or "n/a"),
                rr_mdr,
                _short_case_id(str(row.get("suspected_index_case") or "")),
                recent,
            ])
            cluster_ops_rows.append([
                short_cid,
                status,
                str(row.get("cluster_lead") or "Pending"),
                str(row.get("epi_link_status") or "Pending"),
                str(row.get("setting_type") or "Unknown"),
                str(row.get("contact_tracing_status") or "Not started"),
                str(row.get("ltbi_screening_initiated") or "No"),
                str(row.get("dst_status") or "Pending"),
                str(row.get("recommended_next_step") or "Review at MDT"),
            ])

        cluster_genomic_table = Table(
            wrap_rows(cluster_genomic_rows),
            colWidths=fit_col_widths([0.68*inch, 0.56*inch, 0.82*inch, 0.82*inch, 0.68*inch, 0.68*inch, 0.62*inch, 0.72*inch, 0.95*inch], fill=True),
            repeatRows=1,
        )
        cluster_genomic_table.setStyle(standard_table_style(font_size=7.2, header=True))
        append_table_with_caption(
            cluster_genomic_table,
            "Cluster genomic summary: case counts, specimen date range, SNP distance range, RR/MDR case burden, index case, and recent case counts (30/60/90 days).",
            spacer_after=0.12,
            keep_together=False,
        )

        cluster_ops_table = Table(
            wrap_rows(cluster_ops_rows),
            colWidths=fit_col_widths([0.65*inch, 0.75*inch, 0.88*inch, 0.88*inch, 0.8*inch, 0.9*inch, 0.72*inch, 0.72*inch, 1.0*inch], fill=True),
            repeatRows=1,
        )
        cluster_ops_table.setStyle(standard_table_style(font_size=7.2, header=True))
        append_table_with_caption(
            cluster_ops_table,
            "Cluster operational tracker: investigation lead, epi link status, setting, contact tracing, LTBI screening, DST status, recommended next step. "
            "Fields pre-populated as 'Pending' must be completed by the responsible clinician or investigator before MDT circulation.",
            spacer_after=0.0,
            keep_together=False,
        )
    else:
        story.append(Paragraph("No cluster-level epidemiology rows available.", styles["Normal"]))

    # ── Cluster Growth Status ──────────────────────────────────────────────────
    section_divider()
    append_section_heading("Cluster Growth Status")
    story.append(Paragraph(
        "Cluster growth status classifies whether each cluster is actively accumulating new cases based on recent specimen dates. "
        "Active clusters (last case <90 days ago) require heightened investigation priority. "
        "Clusters with no new cases for >180 days may be approaching natural closure but require formal MDT sign-off.",
        interp_style,
    ))
    import datetime as _dt
    _today = _dt.date.today()
    _growth_rows = [["Cluster", "Cases", "Last case", "Cases 30d", "Cases 60d", "Cases 90d", "Growth status"]]
    for _cr in cluster_epi_rows:
        _cid = _short_case_id(str(_cr.get("cluster_id") or ""))
        _n_cases = int(_cr.get("cases") or 0)
        _latest_raw = _cr.get("latest_specimen")
        # Handle date objects or strings
        if _latest_raw is None:
            _latest_str = "n/a"
            _days_since = None
        elif hasattr(_latest_raw, "isoformat"):
            _latest_str = _latest_raw.isoformat()[:10]
            try:
                _days_since = (_today - _latest_raw.date() if hasattr(_latest_raw, "date") else _today - _latest_raw).days
            except Exception:
                _days_since = None
        else:
            _latest_str = str(_latest_raw)[:10]
            try:
                _days_since = (_today - _dt.date.fromisoformat(_latest_str)).days
            except Exception:
                _days_since = None
        _r30 = int(_cr.get("recent_30d") or 0)
        _r60 = int(_cr.get("recent_60d") or 0)
        _r90 = int(_cr.get("recent_90d") or 0)
        if _days_since is None:
            _status = "Unknown"
        elif _days_since < 90:
            _status = "Active"
        elif _days_since < 180:
            _status = "Slowing"
        else:
            _status = "Likely inactive"
        _growth_rows.append([_cid, str(_n_cases), _latest_str, str(_r30), str(_r60), str(_r90), _status])
    if len(_growth_rows) > 1:
        _growth_tbl = Table(
            wrap_rows(_growth_rows),
            colWidths=fit_col_widths([0.9*inch, 0.6*inch, 1.05*inch, 0.82*inch, 0.82*inch, 0.82*inch, 1.15*inch], fill=True),
            repeatRows=1,
        )
        _growth_tbl.setStyle(standard_table_style(font_size=7.5, header=True))
        append_table_with_caption(
            _growth_tbl,
            "Cluster growth status. Cases 30d/60d/90d = cases with specimen date within that window of today. "
            "Active = last case <90 days ago; Slowing = 90–180 days; Likely inactive = >180 days. "
            "Status does not imply transmission has stopped; formal closure requires MDT sign-off against cluster closure criteria.",
            spacer_after=0.0,
            keep_together=False,
        )
    else:
        story.append(Paragraph("No cluster growth data available.", styles["Normal"]))

    # ── Epi-Link Evidence Summary ──────────────────────────────────────────────
    section_divider()
    append_section_heading("Epi-Link Evidence Summary")
    story.append(Paragraph(
        "Genomic cluster membership is necessary but not sufficient for establishing a transmission chain. "
        "Epidemiological links — shared contacts, common exposure settings, overlapping timelines — provide independent corroboration. "
        "The table below summarises epi-link status for all model-prioritised pairs from this run's output.",
        interp_style,
    ))
    # Corroborated = explicit epi link present; NOT corroborated = unknown/not confirmed/not recorded/none/empty
    _UNCONFIRMED_EPI = {"unknown", "not recorded", "none", "no link", "not confirmed", ""}
    _epi_bins: dict = {}
    _epi_corroborated = 0
    _epi_total = 0
    _epi_path = _export_path("transmission_pairs.csv")
    if os.path.exists(_epi_path):
        import csv as _csv2
        with open(_epi_path, newline="", encoding="utf-8") as _ef:
            for _epi_row in _csv2.DictReader(_ef):
                _epi_link = str(_epi_row.get("Epi link") or "not recorded").strip().lower()
                _epi_bins[_epi_link] = _epi_bins.get(_epi_link, 0) + 1
                _epi_total += 1
                if _epi_link not in _UNCONFIRMED_EPI:
                    _epi_corroborated += 1
    _epi_unconfirmed = _epi_total - _epi_corroborated
    _epi_summary_rows = [["Epi-link category", "Pair count", "% of all pairs", "Corroborated?"]]
    for _elink, _ecount in sorted(_epi_bins.items(), key=lambda x: -x[1]):
        _epi_pct = f"{round(100 * _ecount / _epi_total, 1)}%" if _epi_total else "n/a"
        _is_corroborated = "No" if _elink in _UNCONFIRMED_EPI else "Yes"
        _epi_summary_rows.append([_elink.title(), str(_ecount), _epi_pct, _is_corroborated])
    if not _epi_bins:
        _epi_summary_rows.append(["No epi-link data found in this extract", "—", "—", "—"])
    _epi_tbl = Table(
        wrap_rows(_epi_summary_rows),
        colWidths=fit_col_widths([2.1*inch, 0.85*inch, 0.85*inch, 0.85*inch], fill=True),
        repeatRows=1,
    )
    _epi_tbl.setStyle(standard_table_style(font_size=7.5, header=True))
    story.append(_epi_tbl)
    if _epi_total > 0:
        story.append(Spacer(1, 0.06 * inch))
        _epi_corr_pct = round(100 * _epi_corroborated / _epi_total, 1)
        story.append(Paragraph(
            f"Recorded epi-status available for {_epi_total}/{_epi_total} pairs. "
            f"Corroborated epi-link present for {_epi_corroborated}/{_epi_total} pairs ({_epi_corr_pct}%). "
            f"{_epi_unconfirmed} pair{'s' if _epi_unconfirmed != 1 else ''} remain not confirmed and require "
            "field investigation before operational escalation.",
            small_style,
        ))

    # ── Contact-Tracing Yield ──────────────────────────────────────────────────
    section_divider()
    append_section_heading("Contact-Tracing Yield — pending field data")
    story.append(Paragraph(
        "<b>Contact-tracing yield: not available in current extract.</b> "
        "Contact tracing data are not recorded in this system. "
        "Field investigation teams should record: contacts identified, contacts screened, active TB cases found, LTBI cases found, and yield percentage per index cluster. "
        "Re-ingest after populating the case management system to enable automated yield reporting here. "
        "The expected table structure when data are available is shown below.",
        section_note_style,
    ))
    _ct_header_rows = [
        ["Cluster", "Contacts identified", "Contacts screened", "Active TB", "LTBI", "Yield %", "Completion"],
        ["(pending)", "—", "—", "—", "—", "—", "Awaiting field data"],
    ]
    _ct_tbl = Table(
        wrap_rows(_ct_header_rows),
        colWidths=fit_col_widths([0.82*inch, 1.2*inch, 1.15*inch, 0.75*inch, 0.65*inch, 0.68*inch, 1.25*inch], fill=True),
        repeatRows=1,
    )
    _ct_ts = standard_table_style(font_size=7.2, header=True)
    _ct_ts.add("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#888888"))
    _ct_tbl.setStyle(_ct_ts)
    story.append(_ct_tbl)
    story.append(Spacer(1, 0.04 * inch))
    story.append(Paragraph(
        "Yield % = (active TB + LTBI) / contacts screened × 100. "
        "Completion = proportion of contacts with outcome recorded.",
        small_style,
    ))

    section_divider()
    append_section_heading("Geographical Cluster Spread")
    if cluster_epi_rows:
        _UK_ONLY_LABELS = {"united kingdom", "uk", "unknown"}
        geo_rows = [["Cluster", "Regions", "Cases", "Cross-Region", "Risk Flag"]]
        geo_note_shown = False
        for row in cluster_epi_rows[:20]:
            cluster_id = str(row.get("cluster_id") or "")
            members = sequence_cluster_members.get(cluster_id, [])
            raw_regions = sorted({str((case_by_id.get(x) or {}).get("geographic_region") or "Unknown") for x in members})
            # Normalise: if all values are UK-level or Unknown, flag as unavailable
            normalised = [r for r in raw_regions if r.lower() not in _UK_ONLY_LABELS]
            if not normalised:
                region_text = "Regional geography unavailable in this extract"
                geo_note_shown = True
            else:
                region_text = ", ".join(raw_regions[:2]) + ("..." if len(raw_regions) > 2 else "")
            cross_region = "yes" if len(raw_regions) > 1 else "no"
            risk_flag = "high" if int(row.get("mdr_cases") or 0) > 0 or int(row.get("recent_30d") or 0) >= 2 else "monitor"
            geo_rows.append([
                _short_case_id(cluster_id),
                region_text,
                str(int(row.get("cases") or 0)),
                cross_region,
                risk_flag,
            ])
        geo_table = Table(wrap_rows(geo_rows), colWidths=[0.72 * inch, 2.45 * inch, 0.58 * inch, 0.78 * inch, 0.78 * inch], repeatRows=1)
        geo_table.setStyle(standard_table_style(font_size=7.5, header=True))
        append_table_with_caption(
            geo_table,
            "Cluster geography summary at region/trust-safe level (no household-level identifiers displayed).",
            spacer_after=0.0,
            keep_together=False,
        )
        if geo_note_shown:
            story.append(Paragraph(
                "Regional geography unavailable in this extract; data is recorded at United Kingdom level only. "
                "For operational surveillance, re-ingest with a safe hierarchy in geographic_region: HSC Trust, PHA locality, council area, and postcode district only if governance approvals and small-number controls are satisfied.",
                section_note_style,
            ))

    section_divider()
    append_section_heading("Missing-Data Dashboard")
    if completeness_row:
        total = int(completeness_row.get("total_cases") or 0)

        def pct(present):
            return round((int(present or 0) / total) * 100.0, 1) if total else 0.0

        miss_rows = [["Field", "Completeness", "Missing", "Impact"]]
        field_specs = [
            ("Collection date", completeness_row.get("specimen_date_present"), "affects outbreaker2 timing"),
            ("Notification date (proxy)", completeness_row.get("notification_proxy_present"), "affects surveillance timeline"),
            ("Region/trust", completeness_row.get("region_present"), "affects representativeness and spread"),
            ("Lineage", completeness_row.get("lineage_present"), "affects phylogenetic interpretation"),
            ("Resistance call", completeness_row.get("resistance_present"), "affects clinical action"),
            ("QC metrics", completeness_row.get("qc_present"), "affects confidence in inference"),
        ]
        for label, present, impact in field_specs:
            present_n = int(present or 0)
            miss_rows.append([
                label,
                f"{pct(present_n)}%",
                str(max(total - present_n, 0)),
                impact,
            ])

        miss_table = Table(wrap_rows(miss_rows), colWidths=[1.3 * inch, 0.95 * inch, 0.65 * inch, 2.95 * inch], repeatRows=1)
        miss_table.setStyle(standard_table_style(font_size=8.0, header=True))
        append_table_with_caption(
            miss_table,
            "Table 23. Completeness and impact dashboard for key outbreak-investigation data fields, aligned to transparent inference limitations.",
            spacer_after=0.0,
        )

    # ── Cluster Closure Criteria ───────────────────────────────────────────────
    section_divider()
    append_section_heading("Cluster Closure Criteria")
    story.append(Paragraph(
        "Clusters should not be closed on genomic evidence alone. The following criteria define the minimum standards for cluster closure, "
        "based on ECDC/PHE TB cluster management guidance. "
        "All criteria should be met before a cluster is formally closed at MDT. Closure is documented with MDT sign-off and rationale.",
        interp_style,
    ))
    _closure_rows = [
        ["Criterion", "Standard", "How to assess", "Met in this report?"],
        ["No new linked cases",
         "No genomically linked new case within preceding 12 months",
         "Review cluster growth status table; confirm last case >365 days ago",
         "Review cluster growth status table"],
        ["All contacts traced",
         "All known contacts identified, assessed, and outcome recorded",
         "Contact-tracing yield table shows 100% contacts assessed",
         "Contact tracing data not recorded in this extract"],
        ["Index case identified or excluded",
         "Probable index case or documented exclusion of identifiable source",
         "Transmission network and MDT review",
         "Probable index case may be noted in cluster operational tracker"],
        ["Transmission links resolved",
         "All SNP-linked pairs adjudicated (epi confirmed or excluded)",
         "Epi-link evidence summary: all pairs have documented epi outcome",
         "Epi-link data partially available — see epi-link summary section"],
        ["Drug resistance resolved",
         "Phenotypic DST completed for all resistance-predicted cases",
         "Phenotypic DST reconciliation table: no 'Awaiting' entries",
         "Phenotypic DST not recorded in this extract"],
        ["MDT sign-off documented",
         "Formal MDT discussion with closure rationale recorded",
         "Case management system or minutes reference",
         "Out of scope for automated report"],
    ]
    _closure_tbl = Table(
        wrap_rows(_closure_rows),
        colWidths=fit_col_widths([1.35*inch, 1.6*inch, 1.9*inch, 1.9*inch], fill=True),
        repeatRows=1,
    )
    _closure_ts = standard_table_style(font_size=7.0, header=True, valign_top=True)
    _closure_ts.add("TOPPADDING", (0, 0), (-1, -1), 2)
    _closure_ts.add("BOTTOMPADDING", (0, 0), (-1, -1), 2)
    _closure_tbl.setStyle(_closure_ts)
    story.append(_closure_tbl)
    story.append(Spacer(1, 0.06 * inch))
    story.append(Paragraph(
        "Cluster closure must be documented in the case management system with date, responsible clinician, and rationale. "
        "Genomic surveillance should continue after closure to detect re-emergence.",
        small_style,
    ))

    section_divider("Diagnostics and figures")
    append_section_heading("Model Reliability Diagnostics")
    story.append(Paragraph(
        "MCMC convergence diagnostics indicate whether the Bayesian sampler has explored the parameter space adequately. "
        "Convergence diagnostic (Gelman-Rubin / R-hat) <1.1 indicates reliable estimates. Values >1.1 suggest exploratory inference only. "
        "Effective sample size, acceptance rate, and multi-chain robustness all support confidence in the transmission probabilities reported. "
        "For robust surveillance use: fixed random seed, multiple chains, longer run length, reported R-hat/ESS/acceptance rate, "
        "and sensitivity analysis using plausible TB generation-time priors.",
        section_note_style,
    ))
    if summary_data:
        conv = summary_data.get("convergence_diagnostic")
        acc = summary_data.get("acceptance_rate")
        ess = summary_data.get("effective_sample_size")
        chains = summary_data.get("n_chains")
        sensitivity = summary_data.get("sensitivity_run")

        conv_val = float(conv) if conv is not None else None
        ess_val = int(ess) if ess is not None else None
        acc_val = float(acc) if acc is not None else None
        chains_val = int(chains) if chains is not None else None
        
        # Interpretation logic
        conv_interpretation = "Converged: reliable estimates" if conv_val is not None and conv_val <= 1.1 else ("Did not converge: treat inferences as exploratory" if conv_val is not None else "Not available")
        ess_interpretation = "Good (robust estimates)" if ess_val is not None and ess_val > 200 else ("Low (consider wider confidence intervals)" if ess_val is not None else "Not available")
        acc_interpretation = "Optimal mixing" if acc_val is not None and 0.2 <= acc_val <= 0.6 else ("Possible tuning issue: rerun with parameter adjustment" if acc_val is not None else "Not available")
        
        reliability = "Operationally Reliable" if conv_val is not None and conv_val <= 1.1 else "Exploratory / Cautionary"

        diag_rows = [
            ["Diagnostic", "Value", "Status", "Meaning"],
            ["Convergence (R-hat)", f"{conv_val:.3f}" if conv_val is not None else "n/a", "✓ Pass" if conv_val is not None and conv_val <= 1.1 else "⚠ Review", conv_interpretation],
            ["Effective sample size (ESS)", str(ess_val if ess_val is not None else "n/a"), "✓ Adequate" if ess_val is not None and ess_val > 200 else "⚠ Limited", ess_interpretation],
            ["Acceptance rate", f"{acc_val:.2%}" if acc_val is not None else "n/a", "✓ Optimal" if acc_val is not None and 0.2 <= acc_val <= 0.6 else "⚠ Check", acc_interpretation],
            ["Parallel chains", str(chains_val if chains_val is not None else "n/a"), "✓ Multi-chain" if chains_val is not None and chains_val >= 2 else "○ Single-chain", "Multi-chain provides robustness" if chains_val is not None and chains_val >= 2 else "Single-chain: less robust"],
            ["Sensitivity analysis", str(sensitivity if sensitivity is not None else "Not performed").title(), "✓ Yes" if sensitivity else "○ No", "Confirms results stable across parameter priors"],
        ]
        diag_table = Table(wrap_rows(diag_rows), colWidths=[1.4 * inch, 0.9 * inch, 0.8 * inch, 2.6 * inch], repeatRows=1)
        diag_table.setStyle(standard_table_style(font_size=7.8, header=True))
        append_table_with_caption(
            diag_table,
            "Table 24. MCMC convergence diagnostics. Use exploratory language for transmission interpretation if convergence R-hat >1.1. "
            "All reported posterior probabilities should be interpreted with awareness of these diagnostic results.",
            spacer_after=0.0,
        )
        
        if conv_val is not None and conv_val > 1.1:
            story.append(Spacer(1, 0.08 * inch))
            story.append(Paragraph(
                f"<b>⚠ MODEL CAVEAT:</b> Convergence diagnostic R-hat = {conv_val:.3f} (>1.1 threshold). "
                "MCMC has not fully mixed. All transmission probabilities and network inferences should be treated as exploratory. "
                "Consider: (1) fixed random seed, (2) multiple chains, (3) increased chain length, (4) reporting ESS and acceptance rate, "
                "(5) sensitivity analysis with plausible TB generation-time priors, (6) consulting bioinformatics team before operational decisions.",
                interp_style
            ))
    else:
        story.append(Paragraph(
            "No MCMC diagnostic data available. Model reliability cannot be assessed. Treat all inferences as exploratory and rerun with fixed seed, multiple chains, longer MCMC run, reported R-hat/ESS/acceptance rate, and generation-time-prior sensitivity analysis.",
            interp_style,
        ))

    section_divider()
    append_section_heading("Transmission Routes Reference")
    # ── TB Transmission Routes — Background ───────────────────────────────────
    append_section_heading("Understanding TB Transmission Routes from WGS")
    story.append(Paragraph(
        "Whole-genome sequencing identifies genomic relatedness but does not directly observe contact events. "
        "Combining genomic clusters with epidemiological data (contact tracing, shared locations, timeline of "
        "diagnosis) allows investigators to build a plausible transmission chain. The table below summarises "
        "the genomic signals and their transmission implications.",
        interp_style,
    ))
    routes_rows = [
        [Paragraph("Genomic Signal", cell_hdr_style), Paragraph("SNP Range", cell_hdr_style),
         Paragraph("Transmission Implication", cell_hdr_style), Paragraph("Recommended Action", cell_hdr_style)],
        [Paragraph("Highly probable direct transmission", cell_bold_style), Paragraph("0\u20135 SNPs", cell_body_style),
         Paragraph("Strong genomic evidence of recent direct person-to-person transmission. Strain has had little time to evolve.", cell_body_style),
         Paragraph("Immediate contact tracing; identify shared setting (household, workplace, healthcare).", cell_body_style)],
        [Paragraph("Possible direct or near-direct transmission", cell_bold_style), Paragraph("6\u201312 SNPs", cell_body_style),
         Paragraph("Genetically close; consistent with transmission within the last 1\u20133 years or via an undetected intermediate.", cell_body_style),
         Paragraph("Epidemiological linkage investigation; check whether cases share contacts or settings.", cell_body_style)],
        [Paragraph("Within extended cluster \u2014 indirect link likely", cell_bold_style), Paragraph("13\u201350 SNPs", cell_body_style),
         Paragraph("Genetically related but too diverged for recent direct transmission. Likely share a common ancestor strain.", cell_body_style),
         Paragraph("Review cluster history; may represent reactivation from the same source years earlier.", cell_body_style)],
        [Paragraph("Unrelated strains", cell_bold_style), Paragraph(">50 SNPs", cell_body_style),
         Paragraph("No plausible genomic link. Coincident diagnoses are likely due to independent exposure or reactivation.", cell_body_style),
         Paragraph("No cluster-based action; manage as separate cases.", cell_body_style)],
        [Paragraph("Mixed-lineage / contamination", cell_bold_style), Paragraph("N/A", cell_body_style),
         Paragraph("Two or more distinct strain signals in a single sample. May indicate laboratory cross-contamination or mixed infection.", cell_body_style),
         Paragraph("Flag for repeat sequencing; do not use in cluster assignments until resolved.", cell_body_style)],
    ]
    routes_table = Table(
        routes_rows,
        colWidths=fit_col_widths([1.7 * inch, 0.9 * inch, 2.45 * inch, 2.25 * inch], fill=True),
    )
    routes_table.setStyle(standard_table_style(font_size=7.1, header=True, valign_top=True))
    story.append(routes_table)
    append_numbered_caption(
        "Table 20. TB transmission route classification by SNP distance (M. tuberculosis whole-genome comparison). "
        "SNP thresholds follow UK NICE guideline NG33 and published literature (Walker et al. 2013, Meehan et al. 2019). "
        "The generation-time prior used in outbreaker2 is programme-configured and influences whether "
        "a given SNP distance is interpreted as consistent with direct versus indirect transmission."
    )
    story.append(Spacer(1, 0.18 * inch))

    append_section_heading("Transmission Priority Signals")
    if transmission_data and transmission_data.get("key_nodes"):
        priority_rows = [["Case", "Region", "Risk", "Out", "In"]]
        for node in transmission_data.get("key_nodes", [])[:10]:
            priority_rows.append([
                str(node.get("case_id", "")),
                str(node.get("region", "")),
                str(node.get("risk_score", "n/a")),
                str(node.get("outgoing_links", 0)),
                str(node.get("incoming_links", 0)),
            ])
        priority_table = Table(wrap_rows(priority_rows), colWidths=[1.25 * inch, 1.9 * inch, 1.0 * inch, 0.8 * inch, 0.8 * inch])
        priority_table.setStyle(standard_table_style(font_size=8.6, header=True))
        story.append(priority_table)

        append_numbered_caption(
            f"Table 21. Top-priority cases by network centrality. "
            f"Network snapshot: {transmission_data.get('node_count', 0)} nodes, "
            f"{transmission_data.get('edge_count', 0)} directed links, "
            f"{high_confidence_all_count} model-prioritised links (posterior >0.70). "
            "<b>Out</b> = outgoing transmission links (potential sources); <b>In</b> = incoming links (potential recipients). "
            "Cases with multiple outgoing model-prioritised links should be treated as potential source nodes requiring validation, not confirmed sources. "
            f"Counts may differ where links are filtered for display, QC status, or posterior thresholding. "
            f"Executive summary count reflects all model links ({high_confidence_all_count}); network snapshot JSON count is {high_confidence_snapshot_count}."
        )
        story.append(Paragraph(
            "Model-prioritised links with posterior probability >0.70 represent statistical transmission hypotheses from outbreaker2. "
            "In this run, they should not be treated as genomic evidence of direct transmission unless supported by pairwise SNP distance, "
            "QC pass status, and epidemiological corroboration.",
            section_note_style,
        ))
    else:
        story.append(Paragraph("No transmission priority data found.", styles["Normal"]))

    story.append(Spacer(1, 0.1 * inch))
    legend_rows = [
        ["Visual element", "Operational meaning"],
        ["Node colour", "Cluster assignment (same colour = same cluster context)"],
        ["Red border", "QC unresolved (failed, contaminated, or not reported)"],
        ["RR/MDR/FQ marker", "Predicted resistance signal; confirm by phenotypic DST"],
        ["Solid edge", "Pairwise SNP ≤12 and QC pass (supported candidate link)"],
        ["Dotted edge", "Pairwise SNP >12 (unlikely direct recent transmission)"],
        ["Grey edge", "Pairwise SNP unavailable (model-only hypothesis)"],
    ]
    legend_table = Table(
        wrap_rows(legend_rows),
        colWidths=[1.6 * inch, 4.2 * inch],
        repeatRows=1,
    )
    legend_table.setStyle(standard_table_style(font_size=7.6, header=True, valign_top=True))
    append_table_with_caption(
        legend_table,
        "Recommended visual encoding for future network outputs. Note: not all elements below may be visible in the current network figure depending on pipeline configuration. This encoding should be applied when network graphs are regenerated with full SNP and QC data.",
        spacer_after=0.0,
    )

    section_divider()
    append_section_heading("Diagnostic Graphics")
    story.append(Paragraph(
        "The following plots are generated by outbreaker2 and the platform's supplementary visualisation pipeline. "
        "Each figure caption explains the content and how to interpret the output.",
        interp_style,
    ))
    story.append(Spacer(1, 0.08 * inch))

    FIGURE_CAPTIONS = {
        "outbreaker_trace.png": (
            "Figure 2. MCMC trace plot — log-posterior probability over iterations. "
            "In this run, the trace does not clearly demonstrate stable mixing; outbreaker2-derived directionality should therefore be treated as exploratory pending repeat runs with longer chains and formal convergence diagnostics."
        ),
        "outbreaker_hist.png": (
            "Figure 3. Posterior distribution histograms — marginal distributions of key model parameters "
            "(transmission probability, sampling probability, generation time). "
            "Narrow, symmetric peaks indicate well-determined parameters. Broad or multi-modal distributions "
            "suggest parameter uncertainty, which should be reflected in cautious interpretation of individual "
            "transmission links."
        ),
        "outbreaker_tree.png": (
            "Figure 4. Inferred transmission tree (most probable who-infected-whom). "
            "Each node is a case; arrows indicate the direction of inferred transmission from source (tail) "
            "to recipient (head). Arrow thickness or colour (where shown) reflects posterior probability. "
            "Dashed or thin arrows indicate lower-confidence links. Cases with no incoming arrow are "
            "probable index cases or represent undetected importation events."
        ),
        "outbreaker_phylo.png": (
            "Figure 5. Phylogenetic context — a midpoint-rooted maximum parsimony or neighbour-joining tree "
            "of sequenced cases, coloured by cluster or region. "
            "Branch length represents SNP distance. Cases on short branches with few SNPs between them "
            "form tight clades consistent with recent transmission. Well-separated clades indicate "
            "genetically distinct strain lineages circulating concurrently."
        ),
        "outbreaker_resistance.png": (
            "Figure 6. Drug resistance profile summary — frequency of predicted resistance mutations across "
            "the sequenced cohort. "
            "Bars represent the proportion of cases with predicted resistance to each antibiotic class. "
            "Rifampicin + isoniazid co-resistance defines MDR-TB. High frequencies of any first-line "
            "resistance warrant urgent review of empirical treatment protocols."
        ),
        "outbreaker_weekly_trends.png": (
            "Figure 1. 12-week surveillance trend — sequencing coverage (%) and QC pass rate (%) by "
            "calendar week. Coverage is the proportion of eligible culture-confirmed TB cases that received WGS. "
            "Declining coverage weeks may reflect specimen submission delays, laboratory capacity issues, "
            "or data processing backlogs. QC pass rate below 90% in consecutive weeks warrants a "
            "laboratory review."
        ),
    }

    if existing_graphics:
        for fig_num, name in enumerate(existing_graphics, start=2):
            image_path = _export_path(name)
            fig_parts = [build_report_image(image_path)]
            if name in {"outbreaker_tree.png", "outbreaker_phylo.png"}:
                legend_rows = [
                    [Paragraph("<b>Embedded legend</b>", small_style), Paragraph("<b>Encoding</b>", small_style)],
                    [Paragraph("Node colour", small_style), Paragraph("Cluster", small_style)],
                    [Paragraph("Red border", small_style), Paragraph("QC unresolved", small_style)],
                    [Paragraph("Dotted edge", small_style), Paragraph("SNP >12", small_style)],
                    [Paragraph("Grey edge", small_style), Paragraph("SNP unavailable", small_style)],
                    [Paragraph("Solid edge", small_style), Paragraph("SNP ≤12 and QC pass", small_style)],
                    [Paragraph("RR/MDR marker", small_style), Paragraph("Predicted resistance; confirm with DST", small_style)],
                ]
                legend_tbl = Table(
                    legend_rows,
                    colWidths=[1.5 * inch, 3.2 * inch],
                    repeatRows=1,
                )
                legend_tbl.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef4fb")),
                    ("BOX", (0, 0), (-1, -1), 0.6, C_STEEL),
                    ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c8d8ec")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]))
                fig_parts.append(Spacer(1, 0.03 * inch))
                fig_parts.append(legend_tbl)
            caption_text = FIGURE_CAPTIONS.get(name)
            if not caption_text:
                label = name.replace("outbreaker_", "").replace(".png", "").replace("_", " ").title()
                caption_text = f"Figure. {label} — generated by the outbreaker2 analysis pipeline."
            fig_parts.append(Paragraph(caption_text, caption_style))
            story.append(KeepTogether(fig_parts))
            story.append(Spacer(1, 0.1 * inch))
    else:
        story.append(Paragraph("No outbreak graphics found in exports/.", styles["Normal"]))

    # ── Lineage Clinical Reference ─────────────────────────────────────────────
    story.append(Spacer(1, 0.12 * inch))
    lineage_ref_rows = [
        [Paragraph("Lineage", cell_hdr_style), Paragraph("Name / Origin", cell_hdr_style),
         Paragraph("DR Association", cell_hdr_style), Paragraph("NI Relevance", cell_hdr_style),
         Paragraph("Notes", cell_hdr_style)],
        [Paragraph("L1", cell_bold_style), Paragraph("East-African-Indian / Indo-Oceanic", cell_body_style),
         Paragraph("Lower MDR-TB frequency", cell_body_style),
         Paragraph("Cases linked to South Asia, Horn of Africa", cell_body_style),
         Paragraph("Commonly found in Bangladeshi, Indian, Somali communities.", cell_body_style)],
        [Paragraph("L2", cell_bold_style), Paragraph("East-Asian (Beijing lineage)", cell_body_style),
         Paragraph("High MDR/XDR-TB risk; associated with resistance acquisition", cell_body_style),
         Paragraph("Sporadic importation; watch for resistance", cell_body_style),
         Paragraph("Beijing strains have shown high transmissibility in some outbreak settings.", cell_body_style)],
        [Paragraph("L3", cell_bold_style), Paragraph("East-African-Indian (Delhi/CAS)", cell_body_style),
         Paragraph("Moderate DR frequency", cell_body_style),
         Paragraph("Cases linked to Pakistan, Afghanistan, India", cell_body_style),
         Paragraph("Common in large urban TB programmes in the UK.", cell_body_style)],
        [Paragraph("L4", cell_bold_style), Paragraph("Euro-American", cell_body_style),
         Paragraph("Generally lower DR; but historical MDR clusters exist", cell_body_style),
         Paragraph("Dominant UK-born strain type", cell_body_style),
         Paragraph("Most legacy UK TB is L4. Reactivation common in older cohorts.", cell_body_style)],
        [Paragraph("L5 / L6", cell_bold_style), Paragraph("West-African (Mycobacterium africanum)", cell_body_style),
         Paragraph("Lower overall DR", cell_body_style),
         Paragraph("Cases linked to West Africa", cell_body_style),
         Paragraph("Slower growth, may present with atypical features.", cell_body_style)],
        [Paragraph("L7", cell_bold_style), Paragraph("Ethiopian", cell_body_style),
         Paragraph("Limited data", cell_body_style),
         Paragraph("Rare in NI", cell_body_style),
         Paragraph("Emerging lineage classification; limited clinical guidance available.", cell_body_style)],
    ]
    lin_table = Table(
        lineage_ref_rows,
        colWidths=fit_col_widths([0.65 * inch, 1.45 * inch, 1.45 * inch, 1.5 * inch, 2.3 * inch], fill=True),
    )
    lin_table.setStyle(standard_table_style(font_size=7.3, header=True, valign_top=True))
    table_counter["value"] += 1
    story.append(KeepTogether([
        Paragraph("M. tuberculosis Lineage Reference", styles["Heading3"]),
        Paragraph(
            "Lineage classification places a strain within the global phylogeny of M. tuberculosis. "
            "The table below provides context for lineages commonly observed in Northern Ireland.",
            small_style,
        ),
        lin_table,
        Paragraph(
            _format_table_caption(
                "M. tuberculosis lineage reference. DR = drug resistance; MDR = multidrug-resistant; XDR = extensively drug-resistant. "
                "Lineage assignment should be combined with phenotypic DST and clinical judgment. "
                "Source: Coll et al. (2014) Nature Genetics; WHO Global TB Report 2023.",
                table_counter["value"],
            ),
            caption_style,
        ),
    ]))

    # ── Clinical Action Summary ────────────────────────────────────────────────
    section_divider("Governance and action")
    append_section_heading("Clinical and Public Health Action Summary")
    story.append(Paragraph(
        "The table below maps genomic findings to recommended clinical and public health actions. "
        "All actions must be confirmed by the responsible clinician and public health team.",
        interp_style,
    ))
    action_ref_rows = [
        [Paragraph("Finding", cell_hdr_style), Paragraph("Recommended Action", cell_hdr_style),
         Paragraph("Urgency", cell_hdr_style)],
        [Paragraph("New case links to an existing open cluster (\u226412 SNPs)", cell_bold_style),
         Paragraph("Notify cluster lead; extend contact tracing to include new case contacts; "
                   "review whether the cluster source has been identified.", cell_body_style),
         Paragraph("Within 5 working days", cell_body_style)],
        [Paragraph("Model-prioritised link identified (posterior >0.70)", cell_bold_style),
         Paragraph("Review only after confirming pairwise SNP support (\u226412), QC pass status, and epidemiological plausibility. "
               "Do not escalate on model probability alone; document whether SNP and epi evidence corroborates the link.", cell_body_style),
         Paragraph("Within 5 working days", cell_body_style)],
        [Paragraph("New cluster opened (\u22652 cases genetically linked, no prior cluster)", cell_bold_style),
         Paragraph("Open investigation; notify public health; assign epidemiologist; "
                   "initiate contact tracing for all cases.", cell_body_style),
         Paragraph("Within 2 working days", cell_body_style)],
        [Paragraph("MDR-TB predicted by genomics", cell_bold_style),
         Paragraph("Confirm with phenotypic DST; notify MDR-TB specialist centre; "
                   "initiate enhanced infection control if hospitalised.", cell_body_style),
         Paragraph("Immediately on result", cell_body_style)],
        [Paragraph("Sequencing coverage <80% for a region", cell_bold_style),
         Paragraph("Review specimen submission and transport processes for that region; "
                   "identify cases that did not receive WGS and arrange retrospective sequencing if available.", cell_body_style),
         Paragraph("Monthly programme review", cell_body_style)],
        [Paragraph("MCMC convergence diagnostic >1.1", cell_bold_style),
         Paragraph("Do not rely on posterior transmission probabilities from this run. "
                   "Increase MCMC iterations and re-run outbreaker2. Check input data completeness.", cell_body_style),
         Paragraph("Before using results", cell_body_style)],
        [Paragraph("Contamination flag on a sample", cell_bold_style),
         Paragraph("Exclude sample from cluster assignments; arrange repeat sequencing from original culture. "
                   "Investigate laboratory process if multiple consecutive contamination flags.", cell_body_style),
         Paragraph("Within 10 working days", cell_body_style)],
    ]
    act_table = Table(
        action_ref_rows,
        colWidths=fit_col_widths([2.2 * inch, 4.0 * inch, 1.0 * inch], fill=True),
    )
    act_table.setStyle(standard_table_style(font_size=7.2, header=True, valign_top=True))
    story.append(act_table)
    append_numbered_caption(
        "Recommended clinical and public health actions mapped to genomic findings. "
        "Urgency thresholds align with PHE/PHA TB operational guidance. "
        "All genomic findings must be reviewed in conjunction with clinical history, contact tracing records, "
        "and microbiological DST before action is taken.",
        style=caption_style,
    )
    story.append(Paragraph(
        "<b>Disclaimer:</b> Genomic cluster assignments and transmission inferences are probabilistic estimates "
        "based on mathematical models. They supplement but do not replace epidemiological investigation. "
        "Do not use genomic evidence alone to assign legal or clinical liability for transmission.",
        small_style,
    ))

    story.append(Spacer(1, 0.16 * inch))
    append_section_heading("Data Provenance")
    story.append(
        Paragraph(
            "This report combines outbreaker outputs with surveillance KPIs, lineage/DR validation, secondary engine readiness, and cross-method clustering comparison artifacts available at generation time.",
            styles["Normal"],
        )
    )
    if summary_data and summary_data.get("generated_at"):
        story.append(Paragraph(f"Outbreaker summary timestamp: {summary_data.get('generated_at')}", styles["Normal"]))
    if transmission_data and transmission_data.get("generated_at"):
        story.append(Paragraph(f"Transmission network timestamp: {transmission_data.get('generated_at')}", styles["Normal"]))
    if lineage_dr_data and lineage_dr_data.get("generated_at"):
        story.append(Paragraph(f"Lineage/DR validation timestamp: {lineage_dr_data.get('generated_at')}", styles["Normal"]))
    if secondary_validation_data and secondary_validation_data.get("generated_at"):
        story.append(Paragraph(f"Secondary engine validation timestamp: {secondary_validation_data.get('generated_at')}", styles["Normal"]))
    if method_comparison_data and method_comparison_data.get("generated_at"):
        story.append(Paragraph(f"Method comparison timestamp: {method_comparison_data.get('generated_at')}", styles["Normal"]))

    reproducibility_rows = [
        ["Metadata item", "Value"],
        ["Reference genome", str(summary_data.get("reference_genome") if summary_data else None) if (summary_data and summary_data.get("reference_genome")) else "Not recorded in this extract \u2014 mandatory before external circulation (expected: H37Rv / NC_000962.3)"],
        ["SNP-calling pipeline/version", str(summary_data.get("snp_pipeline_version") if summary_data else None) if (summary_data and summary_data.get("snp_pipeline_version")) else "Not recorded in this extract \u2014 mandatory before external circulation (e.g. Clockwork/COMPASS/Snippy + version)"],
        ["Resistance catalogue/version", str(lineage_dr_data.get("resistance_catalogue_version") if lineage_dr_data else None) if (lineage_dr_data and lineage_dr_data.get("resistance_catalogue_version")) else "Not recorded in this extract \u2014 mandatory before external circulation (e.g. WHO mutation catalogue v2)"],
        ["Lineage-calling tool/version", str(lineage_dr_data.get("lineage_tool_version") if lineage_dr_data else None) if (lineage_dr_data and lineage_dr_data.get("lineage_tool_version")) else "Not recorded in this extract \u2014 mandatory before external circulation (e.g. TB-Profiler v4.x / Mykrobe)"],
        ["outbreaker2 version", str(summary_data.get("analysis_engine_version") if summary_data else None) if (summary_data and summary_data.get("analysis_engine_version")) else f"Not recorded \u2014 engine: {summary_data.get('analysis_engine', 'outbreaker2') if summary_data else 'outbreaker2'} (mandatory before external circulation)"],
        ["Random seed", str(summary_data.get("random_seed") if summary_data else None) if (summary_data and summary_data.get("random_seed") is not None) else "Not set \u2014 run is non-reproducible without a fixed seed; mandatory before external circulation"],
        ["Model priors", str(summary_data.get("model_priors") if summary_data else None) if (summary_data and summary_data.get("model_priors")) else (f"Default outbreaker2 priors; MCMC: {summary_data.get('n_generations','?')} generations, burnin {summary_data.get('burnin','?')}, {summary_data.get('n_samples','?')} posterior samples" if summary_data else "Not recorded \u2014 populate before circulation")],
        ["Input hash: exports/cases.csv", _sha256_of_file(_export_path("cases.csv"))],
        ["Input hash: exports/dna.fasta", _sha256_of_file(_export_path("dna.fasta"))],
        ["Input hash: exports/transmission_network.json", _sha256_of_file(_export_path("transmission_network.json"))],
    ]
    reproducibility_table = Table(
        wrap_rows(reproducibility_rows),
        colWidths=[2.2 * inch, 4.4 * inch],
        repeatRows=1,
    )
    reproducibility_table.setStyle(standard_table_style(font_size=7.1, header=True, valign_top=True))
    append_table_with_caption(
        reproducibility_table,
        "Software and reproducibility metadata for governance and audit traceability.",
        spacer_after=0.0,
        keep_together=False,
    )

    # Pre-circulation governance check
    unpopulated_fields = [
        row[0]
        for row in reproducibility_rows[1:]
        if "Not recorded" in str(row[1]) or "Not set" in str(row[1])
    ]
    if unpopulated_fields:
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph(
            f"<b>PRE-CIRCULATION GOVERNANCE NOTICE:</b> {len(unpopulated_fields)} reproducibility field(s) are unpopulated: "
            + "; ".join(unpopulated_fields[:6])
            + (f" (and {len(unpopulated_fields) - 6} more)" if len(unpopulated_fields) > 6 else "")
            + ". External/formal circulation is blocked until these are completed. "
            "Random seed must be recorded to ensure reproducibility. "
            "Contact the pipeline administrator before proceeding.",
            section_note_style,
        ))

    story.append(Spacer(1, 0.06 * inch))
    story.append(Paragraph(
        "<b>Companion files:</b> "
        "exports/appendix_a_case_level_actions.csv \u2014 full case-level actions; "
        "exports/appendix_b_full_discordance_review.csv \u2014 full discordance table; "
        "exports/transmission_pairs.csv \u2014 all model-prioritised pairs with owner/due fields.",
        small_style,
    ))

    # ── APPENDIX ───────────────────────────────────────────────────────────────────────────────
    appendix_started = {"value": False}

    def start_appendix_page():
        if not appendix_started["value"]:
            # Switch from portrait main report to landscape appendix pages.
            story.append(NextPageTemplate("Landscape"))
            story.append(PageBreak())
            appendix_started["value"] = True
        else:
            story.append(PageBreak())

    if case_classif_table_for_appendix or case_actions_b_table_for_appendix:
        start_appendix_page()
        story.append(Paragraph("Appendix A: Case-Level Operational Action Details", styles["Heading2"]))
        story.append(Paragraph(
            "Table A1a \u2014 classification (cluster, SNP, QC, tier).  "
            "Table A1b \u2014 actions (likely link, warning code, action code).  "
            "Full 10-column data: exports/appendix_a_case_level_actions.csv.",
            small_style,
        ))
        story.append(Spacer(1, 0.08 * inch))
        if case_classif_table_for_appendix:
            tbl_obj, _tbl_cap = case_classif_table_for_appendix
            append_appendix_table_block(
                "Table A1a — Case Classification",
                tbl_obj,
                "Table A1a. Case classification: cluster assignment, pairwise SNP availability, nearest-neighbour SNP, outbreaker2 posterior, confidence tier, and QC status.",
            )
        if case_actions_b_table_for_appendix:
            tbl_obj, _tbl_caption = case_actions_b_table_for_appendix
            append_appendix_table_block(
                "Table A1b — Case Actions",
                tbl_obj,
                "Table A1b. Case actions (coded). Full text is in exports/appendix_a_case_level_actions.csv.",
                "<b>Warning codes:</b> "
                "SNP-missing/QC — QC unresolved, pairwise SNP unavailable; "
                "Model-only — outbreaker2 link, no pairwise SNP; "
                "SNP-linked — SNP ≤12, epi corroboration still required; "
                "SNP>12 — pairwise SNP above transmission threshold.  "
                "<b>Action codes:</b> "
                "Repeat/QC — repeat or verify sequence before any transmission interpretation; "
                "Validate SNP+epi — confirm with pairwise SNP and epidemiology before operational action.",
            )

    if discordance_table_for_appendix:
        start_appendix_page()
        story.append(Paragraph("Appendix B: Full Discordance Review", styles["Heading2"]))
        story.append(Paragraph(
            "Discordance codes classify pairs where pairwise SNP evidence and outbreaker2 linkage disagree. "
            "Full data including verbose interpretation is in exports/appendix_b_full_discordance_review.csv.",
            interp_style,
        ))
        story.append(Spacer(1, 0.1 * inch))
        disc_code_rows = [
            [Paragraph("Code", cell_hdr_style), Paragraph("Meaning", cell_hdr_style)],
            [Paragraph("D1", cell_bold_style), Paragraph("Model-linked (outbreaker2 \u22650.70) AND pairwise SNP >12 \u2014 genomically discordant; unlikely direct recent transmission. Do not escalate without further review.", cell_body_style)],
            [Paragraph("D2", cell_bold_style), Paragraph("Model-linked (outbreaker2 \u22650.70) AND pairwise SNP unavailable \u2014 requires sequencing/SNP analysis before transmission interpretation.", cell_body_style)],
            [Paragraph("D3", cell_bold_style), Paragraph("Pairwise SNP \u226412 but NOT model-prioritised \u2014 possible older or shared-source linkage; review epidemiology to determine significance.", cell_body_style)],
        ]
        disc_code_tbl = Table(
            disc_code_rows,
            colWidths=fit_col_widths([0.55 * inch, 9.2 * inch], fill=True, page_width=10.69 * inch),
            repeatRows=1,
        )
        _dct_ts = standard_table_style(font_size=7.5, header=True, valign_top=True)
        _dct_ts.add("TOPPADDING", (0, 0), (-1, -1), 2)
        _dct_ts.add("BOTTOMPADDING", (0, 0), (-1, -1), 2)
        disc_code_tbl.setStyle(_dct_ts)
        append_appendix_table_block(
            "Discordance Code Definitions",
            disc_code_tbl,
            "Table B0. Discordance code definitions used in Table B1.",
        )
        disc_obj, disc_caption = discordance_table_for_appendix
        append_appendix_table_block(
            "Table B1 — Discordant Pairs",
            disc_obj,
            "Table B1. Discordant pairs (coded). Full data including verbose interpretation in exports/appendix_b_full_discordance_review.csv.",
        )

    # Optional one-page MDT action sheet to support rapid governance review.
    start_appendix_page()
    story.append(Paragraph("Appendix C: One-Page MDT Action Sheet", styles["Heading2"]))
    # Reuse the executive-summary high-priority cluster count for appendix consistency.
    mdt_rows = [
        ["Priority area", "Current signal", "Required MDT action", "Owner", "When"],
        ["Circulation readiness", circulation_label, "Complete mandatory reproducibility metadata before external circulation", "Pipeline + Governance", "Before circulation"],
        ["Model reliability", model_reliability, "Treat directionality as exploratory until diagnostics are complete", "Bioinformatics + MDT", "Current cycle"],
        [f"Open clusters: {int(open_clusters or 0)}; high-priority (>10): {high_priority_open_clusters}", f"{int(open_clusters or 0)} total / {high_priority_open_clusters} >10", "Ensure MDT review and epidemiological data completion for all open clusters. Do not infer source-recipient direction from model output alone.", "MDT + Field team", "Next MDT"],
        ["Discordant model links", str(len(discordant_pairs) if discordant_pairs else 0), "Use pairwise SNP + epidemiology adjudication pathway", "MDT", "Next MDT"],
        ["Geography completeness", "UK-only extract", "Populate HSC Trust, PHA locality, council area; use postcode district only with governance approval", "Data management", "Next ingest"],
    ]
    mdt_table = Table(
        wrap_rows(mdt_rows),
        colWidths=fit_col_widths([1.45 * inch, 1.2 * inch, 2.95 * inch, 1.0 * inch, 0.95 * inch], fill=True),
        repeatRows=1,
    )
    mdt_table.setStyle(standard_table_style(font_size=7.4, header=True, valign_top=True))
    append_appendix_table_block(
        "Table C1 — Condensed MDT Action Sheet",
        mdt_table,
        "Table C1. Condensed MDT action sheet for governance and operational review.",
    )

    # ── Appendix D: Key Concepts ───────────────────────────────────────────────
    start_appendix_page()
    story.append(Paragraph("Appendix D: TB Genomics Key Concepts", styles["Heading2"]))
    story.append(Paragraph(
        "Reference definitions for genomic terminology used throughout this report.",
        interp_style,
    ))
    story.append(Spacer(1, 0.1 * inch))
    _bg_table = Table(_key_concepts_rows, colWidths=fit_col_widths([2.0 * inch, 7.6 * inch], fill=True))
    _bg_table.setStyle(standard_table_style(font_size=8.0, header=True, valign_top=True))
    append_appendix_table_block(
        "Table D1 — TB Genomics Reference",
        _bg_table,
        "Table D1. TB genomics reference — key terms (WHO/UK TB genomic surveillance guidance).",
    )

    output_path = report_path
    try:
        doc.build(list(story))
    except PermissionError:
        # If the default report file is open/locked (common on Windows),
        # generate a timestamped filename so report creation still succeeds.
        stamped_name = f"outbreaker_investigation_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
        output_path = _export_path(stamped_name)
        doc = build_doc(output_path)
        doc.build(list(story))

    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename=os.path.basename(output_path),
    )


@router.get("/outbreaker-image/{filename}")
def get_outbreaker_image(filename: str):
    """Serve outbreaker2 generated graphics."""
    path = _export_path(filename)
    
    # Security: only serve expected outbreaker2 images
    if not filename.startswith("outbreaker_") or not filename.endswith(".png"):
        return {"error": "Invalid file"}
    
    if os.path.exists(path):
        return FileResponse(path, media_type="image/png")
    
    return {"error": "Image not found"}


@router.get("/search")
def advanced_search(
    region: str = None,
    lineage: str = None,
    date_from: str = None,
    date_to: str = None,
    resistance: str = None,
    cluster_id: str = None,
    db: Session = Depends(get_db)
):
    """
    Advanced search with multiple filter options.
    
    Parameters:
    - region: Filter by geographic region
    - lineage: Filter by TB lineage (L1, L2, L3, L4)
    - date_from: Filter specimens from this date (YYYY-MM-DD)
    - date_to: Filter specimens until this date (YYYY-MM-DD)
    - resistance: Filter by resistance pattern (R, I, S)
    - cluster_id: Filter by cluster membership
    """
    
    query_str = """
        SELECT DISTINCT
            c.pseudonymised_case_id as case_id,
            c.specimen_date,
            c.geographic_region,
            c.case_status,
            ti.lineage,
            ti.sublineage,
            ti.predicted_drug_resistance,
            cc.cluster_id,
            COUNT(*) OVER (PARTITION BY cc.cluster_id) as cluster_size
        FROM cases c
        LEFT JOIN tb_interpretation ti ON c.pseudonymised_case_id = ti.sample_id
        LEFT JOIN case_clusters cc ON c.pseudonymised_case_id = cc.sample_id
        WHERE 1=1
    """
    
    params = {}
    if region:
        query_str += " AND c.geographic_region = :region"
        params["region"] = region
    if lineage:
        query_str += " AND ti.lineage = :lineage"
        params["lineage"] = lineage
    if resistance:
        query_str += (
            " AND ti.predicted_drug_resistance IS NOT NULL"
            " AND LOWER(CAST(ti.predicted_drug_resistance AS TEXT)) LIKE :resistance_pattern"
        )
        params["resistance_pattern"] = f"%{resistance.strip().lower()}%"
    if date_from:
        query_str += " AND c.specimen_date >= :date_from"
        params["date_from"] = date_from
    if date_to:
        query_str += " AND c.specimen_date <= :date_to"
        params["date_to"] = date_to
    if cluster_id:
        query_str += " AND cc.cluster_id = :cluster_id"
        params["cluster_id"] = cluster_id
    
    query_str += " ORDER BY c.specimen_date DESC LIMIT 100"
    
    results = db.execute(text(query_str), params).mappings().all()
    
    return {
        "filters_applied": {
            "region": region,
            "lineage": lineage,
            "date_range": f"{date_from} to {date_to}" if date_from or date_to else None,
            "resistance": resistance,
            "cluster_id": cluster_id
        },
        "total_results": len(results),
        "cases": [
            {
                "case_id": str(row["case_id"])[:8],
                "specimen_date": str(row["specimen_date"]),
                "region": row["geographic_region"],
                "status": row["case_status"],
                "lineage": row["lineage"],
                "sublineage": row["sublineage"],
                "resistance": row["predicted_drug_resistance"],
                "cluster_id": str(row["cluster_id"])[:8] if row["cluster_id"] else None,
                "cluster_size": row["cluster_size"] if row["cluster_id"] else 0
            }
            for row in results
        ]
    }


@router.get("/case-history/{case_id}")
def get_case_history(case_id: str, db: Session = Depends(get_db)):
    """
    Get case history - all related samples and follow-ups.
    Groups samples by patient/location over time.
    """
    # Handle both full UUID and partial ID (first 8 chars)
    case_id_pattern = f"{case_id}%" if len(case_id) < 36 else case_id
    
    # Find the actual case
    case_result = db.execute(text("""
        SELECT pseudonymised_case_id, geographic_region FROM cases 
        WHERE CAST(pseudonymised_case_id AS TEXT) LIKE :case_id_pattern
        LIMIT 1
    """), {"case_id_pattern": case_id_pattern}).mappings().first()
    
    if not case_result:
        return {"error": "Case not found", "case_id": case_id}
    
    full_case_id = case_result["pseudonymised_case_id"]
    region = case_result["geographic_region"]
    
    # Find all samples from same region or same case
    results = db.execute(text("""
        SELECT 
            c.pseudonymised_case_id,
            c.local_lab_sample_id,
            c.specimen_date,
            c.geographic_region,
            c.case_status,
            ti.lineage,
            ti.predicted_drug_resistance,
            cc.cluster_id
        FROM cases c
        LEFT JOIN tb_interpretation ti ON c.pseudonymised_case_id = ti.sample_id
        LEFT JOIN case_clusters cc ON c.pseudonymised_case_id = cc.sample_id
        WHERE c.pseudonymised_case_id = :case_id
           OR c.geographic_region = :region
        ORDER BY c.specimen_date
    """), {"case_id": full_case_id, "region": region}).mappings().all()
    
    if not results:
        return {"error": "Case not found", "case_id": case_id}
    
    case_history = []
    for row in results:
        case_history.append({
            "case_id": str(row["pseudonymised_case_id"])[:8],
            "specimen_date": str(row["specimen_date"]),
            "region": row["geographic_region"],
            "status": row["case_status"],
            "lineage": row["lineage"],
            "resistance": row["predicted_drug_resistance"],
            "cluster_id": str(row["cluster_id"])[:8] if row["cluster_id"] else None,
            "is_index_case": str(row["pseudonymised_case_id"]) == str(full_case_id)
        })
    
    # Calculate days between first and last
    span_days = 0
    if len(case_history) > 1:
        try:
            dates_str = [h["specimen_date"] for h in case_history]
            dates = sorted(dates_str)
            if dates[0] and dates[-1] and dates[0] != dates[-1]:
                d1 = datetime.strptime(dates[0][:10], "%Y-%m-%d")
                d2 = datetime.strptime(dates[-1][:10], "%Y-%m-%d")
                span_days = abs((d2 - d1).days)
        except Exception:
            span_days = 0
    
    return {
        "case_id": case_id,
        "related_cases": len(case_history),
        "observation_span_days": span_days,
        "history": case_history
    }
