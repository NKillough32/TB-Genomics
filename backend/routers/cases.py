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


@router.get("/summary")
def cases_summary(db: Session = Depends(get_db)):
    """KPI summary used by the GUI banner."""
    total = db.execute(text("SELECT COUNT(*) FROM cases")).scalar() or 0
    clustered = db.execute(
        text("SELECT COUNT(DISTINCT sample_id) FROM case_clusters")
    ).scalar() or 0
    open_clusters = db.execute(
        text("SELECT COUNT(*) FROM clusters WHERE investigation_status = 'open'")
    ).scalar() or 0
    return {
        "total_cases": total,
        "clustered_cases": clustered,
        "unclustered_cases": max(0, total - clustered),
        "open_clusters": open_clusters,
    }


@router.get("/data-safety")
def data_safety(db: Session = Depends(get_db)):
    """Return whether the current dataset is operational or synthetic/demo."""
    total = db.execute(text("SELECT COUNT(*) FROM cases")).scalar() or 0
    # Detect synthetic seed events in the audit log
    try:
        seed_events = db.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE action = 'seed_synthetic_dataset'")
        ).scalar() or 0
    except Exception:
        db.rollback()
        seed_events = 0
    # Heuristic: if any seed event exists, data is non-operational
    operational_safe = seed_events == 0
    return {
        "operational_safe": operational_safe,
        "total_cases": total,
        "synthetic_case_count": total if not operational_safe else 0,
        "synthetic_seed_events": seed_events,
    }


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


def _build_outbreak_report_html(db: Session, full: bool = False) -> str:  # noqa: C901
    """Build a rich, PDF-aligned static HTML outbreak report from database counts and export artifacts."""
    import datetime as _dt

    os.makedirs(_export_path(), exist_ok=True)

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    today = _dt.date.today()

    # ── Basic DB counts ────────────────────────────────────────────────────────
    total_cases = db.execute(text("SELECT COUNT(*) FROM cases")).scalar() or 0
    clustered_cases = db.execute(text("SELECT COUNT(DISTINCT sample_id) FROM case_clusters")).scalar() or 0
    open_clusters = db.execute(
        text("SELECT COUNT(*) FROM clusters WHERE investigation_status = 'open'")
    ).scalar() or 0

    # ── Load export JSON artifacts ─────────────────────────────────────────────
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

    # ── Weekly trends ──────────────────────────────────────────────────────────
    qc_table_exists = db.execute(
        text("SELECT to_regclass('public.sample_qc_metrics') IS NOT NULL")
    ).scalar()
    weekly_trends = []
    try:
        if qc_table_exists:
            weekly_trends = db.execute(text("""
                WITH ww AS (SELECT (DATE_TRUNC('week',CURRENT_DATE)-(s*INTERVAL '7 days'))::date AS ws
                            FROM generate_series(11,0,-1) s),
                wdata AS (SELECT ww.ws,COUNT(c.pseudonymised_case_id)::int AS eligible,
                    COUNT(cs.sample_id)::int AS sequenced,
                    COUNT(sqm.sample_id)::int AS qc_rep,
                    COUNT(*) FILTER(WHERE LOWER(COALESCE(sqm.qc_status,'')) IN ('pass','passed'))::int AS qc_pass
                    FROM ww LEFT JOIN cases c ON c.specimen_date>=ww.ws AND c.specimen_date<ww.ws+INTERVAL '7 days'
                    LEFT JOIN consensus_sequences cs ON cs.sample_id=c.pseudonymised_case_id
                    LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id=c.pseudonymised_case_id
                    GROUP BY ww.ws)
                SELECT ws AS week_start,eligible,sequenced,
                    CASE WHEN eligible=0 THEN NULL ELSE ROUND((sequenced::numeric/eligible::numeric)*100,1) END AS seq_pct,
                    CASE WHEN qc_rep=0 THEN NULL ELSE ROUND((qc_pass::numeric/qc_rep::numeric)*100,1) END AS qc_pct
                FROM wdata ORDER BY ws
            """)).mappings().all()
        else:
            weekly_trends = db.execute(text("""
                WITH ww AS (SELECT (DATE_TRUNC('week',CURRENT_DATE)-(s*INTERVAL '7 days'))::date AS ws
                            FROM generate_series(11,0,-1) s)
                SELECT ww.ws AS week_start,COUNT(c.pseudonymised_case_id)::int AS eligible,
                    COUNT(cs.sample_id)::int AS sequenced,
                    CASE WHEN COUNT(c.pseudonymised_case_id)=0 THEN NULL
                         ELSE ROUND((COUNT(cs.sample_id)::numeric/COUNT(c.pseudonymised_case_id)::numeric)*100,1)
                    END AS seq_pct, NULL::numeric AS qc_pct
                FROM ww LEFT JOIN cases c ON c.specimen_date>=ww.ws AND c.specimen_date<ww.ws+INTERVAL '7 days'
                LEFT JOIN consensus_sequences cs ON cs.sample_id=c.pseudonymised_case_id
                GROUP BY ww.ws ORDER BY ww.ws
            """)).mappings().all()
    except Exception:
        weekly_trends = []

    # ── Case-level detail query ────────────────────────────────────────────────
    case_rows = []
    try:
        case_rows = db.execute(text("""
            SELECT c.pseudonymised_case_id::text AS case_id, c.specimen_date, c.geographic_region,
                c.case_status, cc.cluster_id::text AS cluster_id, cl.snp_distance, cl.investigation_status,
                ti.lineage, ti.predicted_drug_resistance, ti.resistance_mutations, ti.interpretation_summary,
                cs.sequence, sqm.qc_status, sqm.coverage_breadth, sqm.mean_depth,
                sqm.contamination_flag, sqm.ambiguous_base_percent
            FROM cases c
            LEFT JOIN case_clusters cc ON cc.sample_id=c.pseudonymised_case_id
            LEFT JOIN clusters cl ON cl.cluster_id=cc.cluster_id
            LEFT JOIN tb_interpretation ti ON ti.sample_id=c.pseudonymised_case_id
            LEFT JOIN consensus_sequences cs ON cs.sample_id=c.pseudonymised_case_id
            LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id=c.pseudonymised_case_id
            ORDER BY c.specimen_date DESC NULLS LAST, c.pseudonymised_case_id
        """)).mappings().all()
    except Exception:
        case_rows = []

    case_by_id = {str(r.get("case_id")): r for r in case_rows if r.get("case_id")}
    sequence_by_case = {
        str(r.get("case_id")): str(r.get("sequence") or "").strip().upper()
        for r in case_rows if r.get("case_id") and r.get("sequence")
    }
    pairwise_snp_matrix = _pairwise_matrix(sequence_by_case)

    # ── QC status counts ───────────────────────────────────────────────────────
    qc_status_counts = {"pass": 0, "fail": 0, "not_reported": 0, "contamination": 0}
    for r in case_rows:
        st = str(r.get("qc_status") or "not_reported").lower()
        if bool(r.get("contamination_flag")):
            qc_status_counts["contamination"] += 1
        if st in ("pass", "passed"):
            qc_status_counts["pass"] += 1
        elif st in ("", "not_reported", "na", "n/a", "unknown"):
            qc_status_counts["not_reported"] += 1
        else:
            qc_status_counts["fail"] += 1
    excluded_from_outbreaker = sum(
        1 for r in case_rows
        if str(r.get("qc_status") or "").lower() not in ("pass", "passed") or bool(r.get("contamination_flag"))
    )

    # ── Transmission edge data ─────────────────────────────────────────────────
    transmission_edges = (transmission_data or {}).get("edges") or (transmission_data or {}).get("transmission_edges") or []
    best_incoming: dict = {}
    best_outgoing: dict = {}
    outbreaker_pair_prob: dict = {}
    for edge in transmission_edges:
        src = str(edge.get("source") or "")
        tgt = str(edge.get("target") or "")
        if not src or not tgt:
            continue
        prob = float(edge.get("probability") or 0.0)
        if tgt not in best_incoming or prob > best_incoming[tgt]["probability"]:
            best_incoming[tgt] = {"source": src, "probability": prob}
        if src not in best_outgoing or prob > best_outgoing[src]["probability"]:
            best_outgoing[src] = {"target": tgt, "probability": prob}
        pk = tuple(sorted([src, tgt]))
        if pk not in outbreaker_pair_prob or prob > outbreaker_pair_prob[pk]:
            outbreaker_pair_prob[pk] = prob

    high_confidence_edges = [e for e in transmission_edges if float(e.get("probability") or 0.0) >= 0.70]
    high_confidence_all_count = len(high_confidence_edges)
    high_confidence_snapshot_count = int((transmission_data or {}).get("high_confidence_edges", 0) or 0)

    sequence_cluster_members: dict = {}
    for r in case_rows:
        cid = str(r.get("cluster_id") or "")
        cid_case = str(r.get("case_id") or "")
        if cid and cid_case:
            sequence_cluster_members.setdefault(cid, []).append(cid_case)

    nearest_neighbor_snp: dict = {}
    nearest_neighbor_partner: dict = {}
    for case_id, seq in sequence_by_case.items():
        relevant = []
        for other_id in sequence_by_case:
            if other_id == case_id:
                continue
            dist = pairwise_snp_matrix.get(_pair_key(case_id, other_id))
            if dist is not None:
                relevant.append((dist, other_id))
        if relevant:
            bd, bp = sorted(relevant)[0]
            nearest_neighbor_snp[case_id] = int(bd)
            nearest_neighbor_partner[case_id] = bp

    pairwise_links_le_12 = sum(1 for d in pairwise_snp_matrix.values() if d <= 12)
    sequence_pair_set = {pair for pair, d in pairwise_snp_matrix.items() if d <= 12}

    # ── Cluster action priority (from DB) ─────────────────────────────────────
    cluster_action_rows = []
    try:
        cluster_action_rows = db.execute(text("""
            WITH cs AS (SELECT cc.cluster_id,COUNT(*)::int AS case_count,
                MAX(c.specimen_date) AS most_recent_specimen,
                COUNT(DISTINCT c.geographic_region)::int AS region_count,
                COALESCE(cl.investigation_status,'unknown') AS investigation_status,
                EXTRACT(DAY FROM (CURRENT_DATE::timestamp-MAX(c.specimen_date)::timestamp))::int AS recency_days
                FROM case_clusters cc JOIN cases c ON c.pseudonymised_case_id=cc.sample_id
                LEFT JOIN clusters cl ON cl.cluster_id=cc.cluster_id
                GROUP BY cc.cluster_id,cl.investigation_status)
            SELECT cluster_id,case_count,region_count,most_recent_specimen,recency_days,investigation_status,
                ((case_count*2)+(region_count*3)+CASE WHEN recency_days<=14 THEN 3 WHEN recency_days<=30 THEN 2
                 WHEN recency_days<=60 THEN 1 ELSE 0 END+CASE WHEN investigation_status='open' THEN 3 ELSE 0 END)::int AS priority_score
            FROM cs ORDER BY priority_score DESC,case_count DESC,most_recent_specimen DESC LIMIT 10
        """)).mappings().all()
    except Exception:
        cluster_action_rows = []

    # ── Cluster epidemiology ───────────────────────────────────────────────────
    cluster_epi_rows = []
    try:
        cluster_epi_rows = db.execute(text("""
            WITH base AS (SELECT cc.cluster_id::text AS cluster_id,c.pseudonymised_case_id::text AS case_id,
                c.specimen_date,c.geographic_region,COALESCE(cl.investigation_status,'unknown') AS investigation_status,
                cl.snp_distance,LOWER(CAST(ti.predicted_drug_resistance AS text)) AS dr_text
                FROM case_clusters cc JOIN cases c ON c.pseudonymised_case_id=cc.sample_id
                LEFT JOIN clusters cl ON cl.cluster_id=cc.cluster_id
                LEFT JOIN tb_interpretation ti ON ti.sample_id=cc.sample_id),
            idx AS (SELECT DISTINCT ON(cluster_id) cluster_id,case_id AS suspected_index_case
                    FROM base ORDER BY cluster_id,specimen_date ASC NULLS LAST,case_id)
            SELECT b.cluster_id,COUNT(*)::int AS cases,
                MIN(b.specimen_date) AS first_specimen,MAX(b.specimen_date) AS latest_specimen,
                ROUND(AVG(COALESCE(b.snp_distance,0))::numeric,1) AS median_snp_proxy,
                MAX(COALESCE(b.snp_distance,0))::int AS max_snp_proxy,
                COUNT(*) FILTER(WHERE b.dr_text LIKE '%rifamp%' AND (b.dr_text LIKE '%resistant%' OR b.dr_text LIKE '%"r"%'))::int AS rr_cases,
                COUNT(*) FILTER(WHERE b.dr_text LIKE '%rifamp%' AND b.dr_text LIKE '%isoniazid%' AND (b.dr_text LIKE '%resistant%' OR b.dr_text LIKE '%"r"%'))::int AS mdr_cases,
                COUNT(*) FILTER(WHERE b.specimen_date>=CURRENT_DATE-INTERVAL '30 days')::int AS recent_30d,
                COUNT(*) FILTER(WHERE b.specimen_date>=CURRENT_DATE-INTERVAL '60 days')::int AS recent_60d,
                COUNT(*) FILTER(WHERE b.specimen_date>=CURRENT_DATE-INTERVAL '90 days')::int AS recent_90d,
                MAX(b.investigation_status) AS investigation_status, i.suspected_index_case
            FROM base b LEFT JOIN idx i ON i.cluster_id=b.cluster_id
            GROUP BY b.cluster_id,i.suspected_index_case ORDER BY cases DESC,latest_specimen DESC LIMIT 20
        """)).mappings().all()
    except Exception:
        cluster_epi_rows = []

    # ── Mutation validation rows ───────────────────────────────────────────────
    mutation_rows_raw = []
    try:
        mutation_rows_raw = db.execute(text("""
            SELECT sample_id::text AS case_id,resistance_mutations,predicted_drug_resistance
            FROM tb_interpretation WHERE resistance_mutations IS NOT NULL
            AND resistance_mutations::text NOT IN ('null','{}','[]')
            ORDER BY sample_id LIMIT 80
        """)).mappings().all()
    except Exception:
        mutation_rows_raw = []

    # ── Run-level QC ───────────────────────────────────────────────────────────
    run_qc_rows_db = []
    if qc_table_exists:
        try:
            run_qc_rows_db = [dict(r._mapping) for r in db.execute(text("""
                SELECT CAST(reported_at AS DATE) AS run_date,COUNT(*) AS total_samples,
                    SUM(CASE WHEN LOWER(qc_status) IN ('pass','passed') THEN 1 ELSE 0 END) AS pass_count,
                    SUM(CASE WHEN LOWER(qc_status) NOT IN ('pass','passed') THEN 1 ELSE 0 END) AS fail_count,
                    ROUND(CAST(AVG(mean_depth) AS NUMERIC),1) AS mean_depth_avg,
                    ROUND(CAST(AVG(coverage_breadth) AS NUMERIC),1) AS mean_coverage_avg
                FROM sample_qc_metrics WHERE reported_at IS NOT NULL
                GROUP BY CAST(reported_at AS DATE) ORDER BY CAST(reported_at AS DATE) DESC LIMIT 20
            """)).fetchall()]
        except Exception:
            run_qc_rows_db = []

    # ── Reproducibility / pipeline metadata variables ─────────────────────────
    # Pull from DB tables first, fall back to JSON artifact values.
    _seq_run_row: dict = {}
    try:
        _seq_run_row = dict(db.execute(text(
            "SELECT platform, instrument_name, pipeline_version, reference_genome "
            "FROM sequencing_runs ORDER BY created_at DESC NULLS LAST LIMIT 1"
        )).mappings().first() or {})
    except Exception:
        db.rollback()

    _prov_row_db: dict = {}
    try:
        _prov_row_db = dict(db.execute(text(
            "SELECT pipeline_name, pipeline_version, reference_genome, software_versions, parameters "
            "FROM analysis_provenance ORDER BY generated_at DESC NULLS LAST LIMIT 1"
        )).mappings().first() or {})
    except Exception:
        db.rollback()

    def _parse_jsonb(val: object) -> dict:
        """JSONB columns come back from SQLAlchemy as dicts already; strings need loads()."""
        if isinstance(val, dict):
            return val
        if val is None:
            return {}
        try:
            import json as _j
            result = _j.loads(val)
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}

    _sw: dict = _parse_jsonb(_prov_row_db.get("software_versions"))
    _params: dict = _parse_jsonb(_prov_row_db.get("parameters"))

    ref_genome     = (_prov_row_db.get("reference_genome") or _seq_run_row.get("reference_genome")
                      or (summary_data or {}).get("reference_genome"))
    seq_platform   = _seq_run_row.get("platform") or _params.get("platform")
    instrument     = _seq_run_row.get("instrument_name") or _params.get("instrument")
    library_prep   = _params.get("library_prep") or _params.get("library_preparation")
    snp_pipeline   = (_prov_row_db.get("pipeline_name") or _prov_row_db.get("pipeline_version")
                      or (summary_data or {}).get("snp_pipeline_version"))
    mapping_tool   = _sw.get("mapper") or _sw.get("bwa") or _params.get("mapper")
    variant_caller = _sw.get("variant_caller") or _params.get("variant_caller")
    snp_threshold  = str(_params.get("snp_threshold") or 12)
    resist_cat     = (_sw.get("resistance_catalogue") or _params.get("resistance_catalogue")
                      or (lineage_dr_data or {}).get("resistance_catalogue_version"))
    lineage_tool   = (_sw.get("lineage_tool") or _params.get("lineage_tool")
                      or (lineage_dr_data or {}).get("lineage_tool_version"))
    outbreaker_ver = (_sw.get("outbreaker2") or _params.get("outbreaker_version")
                      or (summary_data or {}).get("analysis_engine_version"))
    random_seed    = (str(_params.get("random_seed") or "")
                      or str((summary_data or {}).get("random_seed") or "") or None)
    gen_time_mean  = str(_params.get("gen_time_mean") or _params.get("generation_time_mean") or "")
    gen_time_sd    = str(_params.get("gen_time_sd") or _params.get("generation_time_sd") or "")
    sampling_prob  = str(_params.get("sampling_prob") or _params.get("pi") or "")

    # ── Reproduce metadata gate ────────────────────────────────────────────────
    # Uses the already-resolved DB variables so the DB is the source of truth.
    required_repro_metadata = [
        ("Reference genome", ref_genome),
        ("SNP-calling pipeline/version", snp_pipeline),
        ("Resistance catalogue/version", resist_cat),
        ("Lineage-calling tool/version", lineage_tool),
        ("outbreaker2 version", outbreaker_ver),
        ("Random seed", random_seed),
    ]
    missing_repro = [label for label, v in required_repro_metadata if v is None or (isinstance(v, str) and not v.strip())]
    circulation_ok = not missing_repro

    # ── Lineage epi summary ────────────────────────────────────────────────────
    analysis_summary = _lineage_analysis_summary(db)
    analysis_epi_summary = _lineage_epi_summary(db)

    # ── Graphics ───────────────────────────────────────────────────────────────
    # Metadata for each known figure: stem → (title, interpretive caption)
    _FIGURE_META: dict[str, tuple[str, str]] = {
        "outbreaker_trace": (
            "MCMC Log-Likelihood Trace",
            "Inspect for convergence: a stable horizontal band indicates good chain mixing. "
            "Visible drift, cycles, or sudden jumps suggest poor convergence — "
            "treat all model output as exploratory until convergence is confirmed.",
        ),
        "outbreaker_hist": (
            "MCMC Log-Likelihood Distribution",
            "A near-normal, unimodal histogram indicates the sampler explored the posterior well. "
            "Multi-modal or heavily skewed distributions suggest the chain has not converged — "
            "model-prioritised transmission links should be interpreted with caution.",
        ),
        "outbreaker_tree": (
            "Posterior Transmission Tree",
            "Arrows show the most probable who-infected-whom direction from outbreaker2 posterior "
            "marginal modes. These are probabilistic hypotheses, not confirmed routes. "
            "Validate each link with pairwise SNP distance ≤12 and epidemiological corroboration "
            "before operational action.",
        ),
        "outbreaker_phylo": (
            "Hierarchical Clustering Dendrogram (SNP Distance)",
            "Cases joined at a low branch height share recent common ancestry. "
            "Visible compact sub-trees correspond to transmission clusters. "
            "Branch heights are proportional to pairwise SNP distance — "
            "cases below the 12-SNP threshold are likely directly linked.",
        ),
        "outbreaker_resistance": (
            "Drug Resistance Profile Heatmap",
            "Rows = case isolates, columns = drug classes. "
            "Green = susceptible, yellow = intermediate, red = resistant. "
            "All genomic resistance predictions must be confirmed by phenotypic DST before clinical use.",
        ),
    }

    # Load all PNGs into a dict keyed by stem for contextual placement
    figures_by_stem: dict[str, str] = {}  # stem → base64 data URI
    for image_path in sorted(Path(_export_path()).glob("outbreaker_*.png")):
        uri = _image_data_uri(str(image_path))
        if uri:
            figures_by_stem[image_path.stem] = uri

    def _figure_card(stem: str, show_in_gallery: bool = False) -> str:
        """Render a single figure with interpretive caption, zoom button, and download link."""
        uri = figures_by_stem.get(stem)
        if not uri:
            return f'<p class="muted figure-missing">Figure <em>{_safe_html(stem)}</em> not yet generated. Run the analysis pipeline to produce it.</p>'
        title, caption = _FIGURE_META.get(stem, (stem.replace("outbreaker_", "").replace("_", " ").title(), ""))
        safe_title = _safe_html(title)
        safe_caption = _safe_html(caption)
        dl_name = f"{stem}.png"
        thumb_cls = "fig-thumb" if show_in_gallery else "fig-full"
        return (
            f'<figure class="fig-card {thumb_cls}" data-stem="{_safe_html(stem)}">'
            f'<div class="fig-img-wrap">'
            f'<img src="{uri}" alt="{safe_title}" loading="lazy" class="fig-img" '
            f'     onclick="openLightbox(\'{_safe_html(stem)}\')">'
            f'<button class="fig-zoom-btn" onclick="openLightbox(\'{_safe_html(stem)}\')" '
            f'        title="Click to zoom" aria-label="Zoom {safe_title}">&#x26F6;</button>'
            f'</div>'
            f'<figcaption>'
            f'<strong class="fig-title">{safe_title}</strong>'
            f'{"<p class=fig-caption>" + safe_caption + "</p>" if caption else ""}'
            f'<a class="fig-dl" href="{uri}" download="{_safe_html(dl_name)}" '
            f'   title="Download {safe_title}">&#x2B07; Download</a>'
            f'</figcaption>'
            f'</figure>'
        )

    # Build the full figures gallery (thumbnail grid at bottom of report)
    if figures_by_stem:
        graphics_html = [_figure_card(stem, show_in_gallery=True) for stem in sorted(figures_by_stem.keys())]
    else:
        graphics_html = []

    # ── Pair categorisation ────────────────────────────────────────────────────
    genomic_pairs = []
    model_only_pairs = []
    qc_resolution_pairs = []
    genomically_discordant = []
    for edge in sorted(high_confidence_edges, key=lambda x: float(x.get("probability") or 0.0), reverse=True)[:40]:
        src = str(edge.get("source") or "")
        tgt = str(edge.get("target") or "")
        prob = float(edge.get("probability") or 0.0)
        src_case = case_by_id.get(src) or {}
        tgt_case = case_by_id.get(tgt) or {}
        src_cluster = str(src_case.get("cluster_id") or "")
        tgt_cluster = str(tgt_case.get("cluster_id") or "")
        same_cluster = bool(src_cluster and src_cluster == tgt_cluster)
        pairwise_distance = pairwise_snp_matrix.get(_pair_key(src, tgt))
        src_qc = str(src_case.get("qc_status") or "not_reported")
        tgt_qc = str(tgt_case.get("qc_status") or "not_reported")
        qc_problem = (src_qc.lower() not in ("pass", "passed") or tgt_qc.lower() not in ("pass", "passed")
                      or bool(src_case.get("contamination_flag")) or bool(tgt_case.get("contamination_flag")))
        validation_flag = (
            "SNP-linked" if pairwise_distance is not None and pairwise_distance <= 12 and same_cluster and not qc_problem
            else ("QC-unresolved" if qc_problem
                  else ("D1: SNP>12" if pairwise_distance is not None and pairwise_distance > 12 else "Model-only"))
        )
        record = {
            "pair": f"{_short_case_id(src)}\u2192{_short_case_id(tgt)}",
            "posterior": prob,
            "pairwise": str(pairwise_distance) if pairwise_distance is not None else "n/a",
            "qc": f"{src_qc}/{tgt_qc}",
            "validation_flag": validation_flag,
        }
        if qc_problem:
            qc_resolution_pairs.append(record)
        elif pairwise_distance is not None and pairwise_distance <= 12 and same_cluster:
            genomic_pairs.append(record)
        elif pairwise_distance is not None and pairwise_distance > 12:
            genomically_discordant.append(record)
        else:
            model_only_pairs.append(record)

    # ── Discordant pairs ───────────────────────────────────────────────────────
    discordant_pairs = []
    for pair in sequence_pair_set.union(set(outbreaker_pair_prob.keys())):
        in_seq = pair in sequence_pair_set
        in_out = pair in outbreaker_pair_prob
        if in_seq == in_out:
            continue
        left, right = pair
        post = float(outbreaker_pair_prob.get(pair) or 0.0)
        pairwise_distance = pairwise_snp_matrix.get(pair)
        disc_code = "D1" if in_out and not in_seq and pairwise_distance is not None and int(pairwise_distance) > 12 else ("D2" if in_out and not in_seq else "D3")
        interp = ("Temporal support without pairwise SNP support" if in_out and not in_seq
                  else "Pairwise SNP support without outbreaker linkage")
        discordant_pairs.append({
            "pair": f"{_short_case_id(str(left))}-{_short_case_id(str(right))}",
            "pairwise": pairwise_distance,
            "posterior": post,
            "interpretation": interp,
            "disc_code": disc_code,
        })

    # ── Action CSV rows (for load) ─────────────────────────────────────────────
    row_limit = None if full else 25
    action_rows_csv = _load_export_csv("appendix_a_case_level_actions.csv", limit=row_limit)
    discordance_rows_csv = _load_export_csv("appendix_b_full_discordance_review.csv", limit=row_limit)

    # ── Key nodes & edges from network JSON ────────────────────────────────────
    key_nodes = (transmission_data or {}).get("key_nodes") or []
    network_edges = (transmission_data or {}).get("edges") or (transmission_data or {}).get("transmission_edges") or []

    # ── Report metadata ────────────────────────────────────────────────────────
    report_label = "Full HTML" if full else "Short HTML"
    report_filename = "outbreaker_investigation_report_full.html" if full else "outbreaker_investigation_report.html"

    # ─────────────────────────────────────────────────────────────────────────
    # Helper: render badge chip HTML
    # ─────────────────────────────────────────────────────────────────────────
    def _badge(text: str) -> str:
        cls = {
            "SNP-linked": "badge-green",
            "Model-only": "badge-amber",
            "QC-unresolved": "badge-red",
            "D1: SNP>12": "badge-orange",
        }.get(text, "badge-grey")
        return f'<span class="badge {cls}">{_safe_html(text)}</span>'

    def _progress(value, label="") -> str:
        if value is None:
            return f'<span class="muted">n/a</span>'
        pct = min(100.0, max(0.0, float(value)))
        colour = "#2a9d8f" if pct >= 80 else ("#f4a261" if pct >= 60 else "#e63946")
        return (f'<div class="prog-wrap" title="{label}">'
                f'<div class="prog-bar" style="width:{pct:.1f}%;background:{colour}"></div>'
                f'<span class="prog-label">{pct:.1f}%</span></div>')

    def _metric_card(label: str, value: str, sub: str = "", alert: bool = False) -> str:
        cls = " metric-alert" if alert else ""
        return (f'<div class="metric{cls}"><div class="metric-label">{_safe_html(label)}</div>'
                f'<div class="metric-value">{_safe_html(value)}</div>'
                f'{"<div class=metric-sub>" + _safe_html(sub) + "</div>" if sub else ""}</div>')

    def _kv_rows_html(mapping, fields) -> str:
        if not isinstance(mapping, dict):
            return '<tr><td colspan="2" class="muted">No data available.</td></tr>'
        rows = ""
        for label, key in fields:
            v = mapping.get(key)
            if v is not None:
                rows += f"<tr><th>{_safe_html(label)}</th><td>{_safe_html(str(v))}</td></tr>"
        return rows or '<tr><td colspan="2" class="muted">No entries.</td></tr>'

    def _data_table_html(rows, columns, empty_msg="No data available.") -> str:
        if not rows:
            return f'<p class="muted">{_safe_html(empty_msg)}</p>'
        header = "".join(f"<th>{_safe_html(lbl)}</th>" for lbl, _ in columns)
        body = ""
        for row in rows:
            body += "<tr>" + "".join(f"<td>{_safe_html(str(row.get(k, '')))}</td>" for _, k in columns) + "</tr>"
        return f"<div class='tbl-wrap'><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>"

    def _pair_table_html(records, title, category_class="") -> str:
        if not records:
            return ""
        rows = ""
        for item in records:
            _post = f"{item['posterior']:.3f}"
            rows += (f"<tr><td class='mono'>{_safe_html(item['pair'])}</td>"
                     f"<td>{_safe_html(_post)}</td>"
                     f"<td>{_safe_html(item['pairwise'])}</td>"
                     f"<td>{_safe_html(item['qc'])}</td>"
                     f"<td>{_badge(item['validation_flag'])}</td></tr>")
        return (f"<h4>{_safe_html(title)}</h4>"
                f"<div class='tbl-wrap'><table><thead><tr><th>Pair</th><th>Posterior</th><th>SNP dist</th>"
                f"<th>QC src/rec</th><th>Flag</th></tr></thead><tbody>{rows}</tbody></table></div>")

    # ─────────────────────────────────────────────────────────────────────────
    # Computed summary values
    # ─────────────────────────────────────────────────────────────────────────
    summary_total = int((kpi_data or {}).get("eligible_cases", total_cases))
    summary_sequenced = int((kpi_data or {}).get("sequenced_cases", len(sequence_by_case)))
    seq_pct = (kpi_data or {}).get("sequenced_pct")
    qc_pass_pct = (kpi_data or {}).get("qc_pass_pct")
    summary_coverage = f"{float(seq_pct):.1f}%" if seq_pct is not None else (
        f"{(summary_sequenced/summary_total)*100:.1f}%" if summary_total else "n/a")
    summary_qc_pass = f"{float(qc_pass_pct):.1f}%" if qc_pass_pct is not None else "n/a"

    model_reliability = "Exploratory"
    if summary_data and summary_data.get("convergence_diagnostic") is not None:
        try:
            model_reliability = "Operationally stronger" if float(summary_data["convergence_diagnostic"]) <= 1.1 else "Exploratory"
        except Exception:
            model_reliability = "Exploratory"

    high_priority_open = sum(
        1 for r in cluster_action_rows
        if str(r.get("investigation_status") or "").lower() == "open" and int(r.get("priority_score") or 0) > 10
    )

    # ─────────────────────────────────────────────────────────────────────────
    # CSS
    # ─────────────────────────────────────────────────────────────────────────
    css = """
:root{
  --navy:#1d3557;--steel:#457b9d;--sky:#a8c8e1;--cloud:#eef4f9;
  --teal:#2a9d8f;--teal-bg:#e8f6f4;--alert:#e63946;--alert-bg:#fde8e8;
  --amber:#f4a261;--amber-bg:#fff4ec;--green:#16a34a;--green-bg:#f0fdf4;
  --ink:#1c2b3a;--muted:#5a7080;--rule:#c5d5e4;--bg:#f4f7fb;--card:#fff;
  --sidebar:260px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font-family:system-ui,Arial,sans-serif;line-height:1.5;display:flex;flex-direction:column;min-height:100vh}

/* ── Header ── */
header{background:linear-gradient(135deg,var(--navy) 0%,#254f78 100%);color:#fff;padding:1.6rem 2rem}
header h1{font-size:1.6rem;font-weight:700;letter-spacing:-.02em}
header p{color:#c8ddf0;font-size:.9rem;margin-top:.25rem}
.status-banner{display:inline-block;margin-top:.5rem;padding:.2rem .75rem;border-radius:20px;font-size:.78rem;font-weight:600;letter-spacing:.03em}
.status-draft{background:#e63946;color:#fff}
.status-ready{background:#16a34a;color:#fff}

/* ── Layout ── */
.layout{display:flex;flex:1;align-items:flex-start}

/* ── Sidebar TOC ── */
nav.sidebar{width:var(--sidebar);flex-shrink:0;position:sticky;top:0;max-height:100vh;overflow-y:auto;
  background:var(--navy);color:#c8ddf0;padding:1rem .75rem;font-size:.82rem;scrollbar-width:thin}
nav.sidebar h3{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:#7fa8c8;margin:.9rem 0 .3rem .2rem}
nav.sidebar a{display:block;padding:.28rem .5rem;border-radius:5px;color:#c8ddf0;text-decoration:none;transition:background .15s}
nav.sidebar a:hover,nav.sidebar a.active{background:rgba(255,255,255,.12);color:#fff}
nav.sidebar .sub{padding-left:1rem;font-size:.78rem}

/* ── Main content ── */
main{flex:1;min-width:0;padding:1.4rem 1.6rem 3rem;max-width:1100px}

/* ── Cards ── */
.card{background:var(--card);border:1px solid var(--rule);border-radius:12px;box-shadow:0 4px 14px rgba(21,38,64,.06);margin:1rem 0;padding:1.25rem 1.4rem;scroll-margin-top:1rem}
.card-note{background:var(--teal-bg);border-color:var(--teal)}
.card-warn{background:var(--amber-bg);border-color:var(--amber)}
.card-alert{background:var(--alert-bg);border-color:var(--alert)}

/* ── Section headings ── */
h2{font-size:1.18rem;font-weight:700;color:var(--navy);border-bottom:2px solid var(--rule);padding-bottom:.35rem;margin-bottom:.9rem}
h3{font-size:.98rem;font-weight:700;color:var(--steel);margin:1rem 0 .45rem}
h4{font-size:.88rem;font-weight:600;color:var(--muted);margin:.8rem 0 .35rem}

/* ── Metric dashboard grid ── */
.metrics-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:.7rem;margin:.75rem 0}
.metric{background:var(--cloud);border:1px solid var(--rule);border-radius:10px;padding:.8rem .9rem;position:relative;overflow:hidden}
.metric::before{content:'';position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--teal);border-radius:4px 0 0 4px}
.metric-alert::before{background:var(--alert)}
.metric-label{font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.metric-value{font-size:1.5rem;font-weight:700;color:var(--navy);line-height:1.2;margin:.15rem 0}
.metric-sub{font-size:.73rem;color:var(--muted)}

/* ── Progress bar ── */
.prog-wrap{position:relative;background:#e2eaf3;border-radius:20px;height:14px;overflow:hidden;min-width:80px}
.prog-bar{height:100%;border-radius:20px;transition:width .3s}
.prog-label{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.68rem;font-weight:600;color:var(--ink)}

/* ── Badges ── */
.badge{display:inline-block;padding:.15rem .5rem;border-radius:12px;font-size:.73rem;font-weight:600;white-space:nowrap}
.badge-green{background:var(--green-bg);color:var(--green);border:1px solid #86efac}
.badge-amber{background:var(--amber-bg);color:#c2410c;border:1px solid #fed7aa}
.badge-red{background:var(--alert-bg);color:var(--alert);border:1px solid #fca5a5}
.badge-orange{background:#fff7ed;color:#c2410c;border:1px solid #fdba74}
.badge-grey{background:#f1f5f9;color:#475569;border:1px solid #cbd5e1}

/* ── Tables ── */
.tbl-wrap{overflow-x:auto;margin:.5rem 0}
table{width:100%;border-collapse:collapse;font-size:.83rem;min-width:400px}
th,td{border:1px solid var(--rule);padding:.42rem .6rem;text-align:left;vertical-align:top}
th{background:#dde9f4;color:var(--navy);font-weight:600;font-size:.78rem;white-space:nowrap}
tbody tr:nth-child(even){background:var(--cloud)}
.kv-table th{width:38%;background:var(--cloud);font-weight:600;color:var(--steel)}
.mono{font-family:monospace;font-size:.78rem}

/* ── Collapsible details ── */
details{border:1px solid var(--rule);border-radius:8px;margin:.6rem 0;overflow:hidden}
details summary{padding:.65rem 1rem;background:var(--cloud);cursor:pointer;font-weight:600;color:var(--navy);font-size:.9rem;user-select:none;list-style:none}
details summary::before{content:'▶ ';font-size:.7rem;color:var(--steel)}
details[open] summary::before{content:'▼ '}
details > div{padding:.9rem 1rem}

/* ── Figures ── */
.figures-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1.2rem;margin:.6rem 0}
.fig-card{border:1px solid var(--rule);border-radius:10px;background:#fff;overflow:hidden;display:flex;flex-direction:column;transition:box-shadow .2s}
.fig-card:hover{box-shadow:0 6px 20px rgba(21,38,64,.12)}
.fig-img-wrap{position:relative;background:#f8fafc;overflow:hidden;line-height:0}
.fig-img{width:100%;height:auto;display:block;cursor:zoom-in;transition:transform .25s}
.fig-card:hover .fig-img{transform:scale(1.02)}
.fig-zoom-btn{position:absolute;bottom:.4rem;right:.4rem;background:rgba(29,53,87,.82);color:#fff;border:none;border-radius:6px;padding:.3rem .45rem;font-size:.85rem;cursor:pointer;line-height:1;opacity:0;transition:opacity .2s}
.fig-card:hover .fig-zoom-btn{opacity:1}
.fig-card figcaption{padding:.65rem .75rem .7rem;flex:1;display:flex;flex-direction:column;gap:.25rem}
.fig-title{font-size:.83rem;color:var(--navy);display:block}
.fig-caption{font-size:.75rem;color:var(--muted);line-height:1.45;margin:0}
.fig-dl{font-size:.73rem;color:var(--teal);text-decoration:none;margin-top:auto;align-self:flex-start}
.fig-dl:hover{text-decoration:underline}
.figure-missing{font-style:italic;color:var(--muted);padding:.5rem 0}
.fig-inline{margin:.75rem 0}
.fig-full .fig-img-wrap{max-height:480px;overflow:hidden}
.fig-full .fig-img{object-fit:contain;max-height:480px;width:100%}

/* ── Lightbox ── */
dialog.lb{border:none;border-radius:14px;padding:0;max-width:96vw;max-height:96vh;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.55);background:#111}
dialog.lb::backdrop{background:rgba(0,0,0,.82)}
.lb-inner{position:relative;display:flex;flex-direction:column;max-height:96vh}
.lb-img{max-width:96vw;max-height:82vh;object-fit:contain;display:block;background:#111}
.lb-bar{background:rgba(0,0,0,.75);color:#f0f0f0;display:flex;align-items:center;gap:.75rem;padding:.5rem .8rem;font-size:.82rem}
.lb-caption{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lb-close{background:none;border:1px solid rgba(255,255,255,.35);color:#f0f0f0;border-radius:6px;padding:.25rem .65rem;cursor:pointer;font-size:.82rem}
.lb-close:hover{background:rgba(255,255,255,.15)}
.lb-dl{color:#80d4c8;font-size:.78rem;text-decoration:none;white-space:nowrap}
.lb-dl:hover{text-decoration:underline}
@media(max-width:640px){.figures-grid{grid-template-columns:1fr}}

/* ── Utilities ── */
.muted{color:var(--muted);font-size:.85rem}
pre{white-space:pre-wrap;background:#0f172a;color:#e2e8f0;border-radius:8px;padding:1rem;overflow:auto;font-size:.78rem}
.callout{background:var(--teal-bg);border-left:4px solid var(--teal);border-radius:0 8px 8px 0;padding:.7rem 1rem;margin:.5rem 0;font-size:.87rem}
.callout-warn{background:var(--amber-bg);border-left-color:var(--amber)}
.callout-alert{background:var(--alert-bg);border-left-color:var(--alert)}
.section-note{background:var(--cloud);border:1px solid var(--sky);border-radius:8px;padding:.65rem .9rem;margin:.5rem 0;font-size:.84rem;color:var(--ink)}
.tag{display:inline-flex;align-items:center;gap:.25rem;background:var(--cloud);border:1px solid var(--rule);border-radius:6px;padding:.1rem .45rem;font-size:.72rem;font-weight:600;color:var(--muted);margin:.1rem}

/* ── Print ── */
@media print{
  nav.sidebar{display:none}
  .layout{display:block}
  main{padding:.5rem;max-width:none}
  header{background:white;color:var(--ink);border-bottom:2px solid var(--rule);padding:.8rem 1rem}
  header p{color:var(--muted)}
  .card{box-shadow:none;page-break-inside:avoid;border:1px solid var(--rule)}
  details{border:1px solid var(--rule)}
  details summary{background:white}
  details > div{display:block !important}
  .prog-wrap{border:1px solid var(--rule)}
}

/* ── Responsive ── */
@media(max-width:780px){
  nav.sidebar{display:none}
  main{padding:1rem .75rem}
}
"""

    # ─────────────────────────────────────────────────────────────────────────
    # Denominator counts
    # ─────────────────────────────────────────────────────────────────────────
    denom_notified = int(total_cases)
    denom_sequenced = len(sequence_by_case)
    try:
        denom_culture_pos = db.execute(text("SELECT COUNT(*) FROM consensus_sequences")).scalar() or 0
    except Exception:
        db.rollback()
        denom_culture_pos = 0
    denom_qc_pass = int(qc_status_counts["pass"])
    _model_nodes: set = set()
    for _e in transmission_edges:
        if _e.get("source"):
            _model_nodes.add(str(_e["source"]))
        if _e.get("target"):
            _model_nodes.add(str(_e["target"]))
    denom_model_nodes = len(_model_nodes)

    # ─────────────────────────────────────────────────────────────────────────
    # Additional computed values for new sections
    # ─────────────────────────────────────────────────────────────────────────

    # Population / denominator box HTML
    denom_html = f"""
<div class="tbl-wrap"><table><thead><tr>
  <th>Term</th><th>Definition</th><th>Count</th><th>Notes</th>
</tr></thead><tbody>
  <tr><td>Notified TB cases</td><td>Human cases in surveillance extract</td><td><strong>{_safe_html(str(denom_notified))}</strong></td><td>Source: cases table</td></tr>
  <tr><td>Culture-positive / sequencing-eligible</td><td>Cases with a consensus sequence loaded</td><td><strong>{_safe_html(str(denom_culture_pos))}</strong></td><td>Source: consensus_sequences</td></tr>
  <tr><td>Sequenced samples</td><td>Cases with sequence data in this extract</td><td><strong>{_safe_html(str(denom_sequenced))}</strong></td><td></td></tr>
  <tr><td>QC-pass genomes</td><td>Genomes passing QC — used for SNP clustering</td><td><strong>{_safe_html(str(denom_qc_pass))}</strong></td><td>Fail/contaminated excluded from inference</td></tr>
  <tr><td>outbreaker2 model nodes</td><td>Cases/samples included in transmission model</td><td><strong>{_safe_html(str(denom_model_nodes))}</strong></td><td>From transmission network JSON; 0 = analysis not yet run</td></tr>
</tbody></table></div>"""

    # QC thresholds box HTML
    qc_thresholds_html = """
<div class="tbl-wrap"><table><thead><tr><th>QC parameter</th><th>Pass threshold</th><th>Basis</th></tr></thead><tbody>
  <tr><td>Genome coverage breadth</td><td>&ge;95%</td><td>PHE TB WGS SOP / standard practice</td></tr>
  <tr><td>Mean depth</td><td>&ge;30&times;</td><td>Required for confident SNP calling</td></tr>
  <tr><td>Ambiguous bases (%)</td><td>&le;5%</td><td>High missingness distorts SNP distances</td></tr>
  <tr><td>Contamination</td><td>No mixed-lineage signal</td><td>Mixed lineage = likely contamination or co-infection — exclude pending investigation</td></tr>
  <tr><td>Minimum reads mapped</td><td>Platform-specific (see pipeline version)</td><td>Record in sequencing_runs table</td></tr>
  <tr><td>Exclusion rule</td><td>Any QC fail OR contamination flag = excluded from SNP clustering and outbreaker2</td><td>Conservative to avoid false transmission links</td></tr>
</tbody></table></div>
<div class="callout callout-warn" style="margin-top:.6rem">
  Samples with QC status <em>not reported</em> are treated as unresolved and excluded from cluster inference pending review.
  Thresholds above are defaults — site-specific SOP values override these if recorded in the pipeline provenance.
</div>"""

    # Epidemiological completeness table HTML — derive from case_rows
    epi_fields_check = [
        ("specimen_date",    "Specimen date"),
        ("geographic_region","Geographic region"),
        ("lineage",          "Lineage called"),
        ("predicted_drug_resistance", "Drug resistance called"),
        ("qc_status",        "QC status recorded"),
        ("cluster_id",       "Cluster assigned"),
    ]
    epi_total = len(case_rows) or 1
    epi_complete_html = "<div class='tbl-wrap'><table><thead><tr><th>Field</th><th>Populated</th><th>Missing</th><th>Completeness</th></tr></thead><tbody>"
    for db_key, label in epi_fields_check:
        populated = sum(1 for r in case_rows if r.get(db_key) not in (None, "", "null", "{}", "[]"))
        missing   = epi_total - populated
        pct       = (populated / epi_total) * 100
        alert_style = ' style="background:var(--alert-bg)"' if pct < 80 else ""
        epi_complete_html += (f"<tr{alert_style}><td>{_safe_html(label)}</td><td>{_safe_html(str(populated))}</td>"
                              f"<td>{_safe_html(str(missing))}</td><td>{_progress(pct)}</td></tr>")
    epi_complete_html += "</tbody></table></div>"
    epi_complete_html += """
<div class="callout callout-warn" style="margin-top:.6rem">
  <strong>Missing epi data domains</strong> (not directly capturable from genomic pipeline — require field data completion):<br>
  Demographics (age band, sex, country of birth, time in UK),
  Clinical infectiousness (pulmonary/extrapulmonary, smear status, cavitation, cough duration),
  Exposure setting (household, workplace, hostel, prison, healthcare, congregate setting),
  Contact tracing (named contacts, shared venues, tracing status),
  Vulnerability factors (homelessness, substance use, immunosuppression, migrant health, prison history),
  Timeline (symptom onset, diagnosis, isolation, treatment start, sequencing date).
  Complete these fields in the case management system for MDT review.
</div>"""

    # Methods section HTML
    methods_html = f"""
<div class="tbl-wrap"><table><thead><tr><th>Pipeline component</th><th>Tool / approach</th><th>Version / parameter</th></tr></thead><tbody>
  <tr><td>Sequencing platform</td><td>{_safe_html(seq_platform or 'Not recorded — populate sequencing_runs.platform')}</td><td>{_safe_html(instrument or '—')}</td></tr>
  <tr><td>Library preparation</td><td>{_safe_html(library_prep or 'Not recorded — populate analysis_provenance.parameters')}</td><td>—</td></tr>
  <tr><td>Reference genome</td><td>{_safe_html(ref_genome or 'Not recorded — required')}</td><td>H37Rv recommended (NC_000962.3)</td></tr>
  <tr><td>Read mapping</td><td>{_safe_html(mapping_tool or 'Not recorded')}</td><td>—</td></tr>
  <tr><td>Variant calling</td><td>{_safe_html(variant_caller or 'Not recorded')}</td><td>Exclude PE/PPE and repetitive regions</td></tr>
  <tr><td>SNP clustering threshold</td><td>{_safe_html(snp_threshold or '12 SNPs (default)')}</td><td>NICE guideline / PHE SOP</td></tr>
  <tr><td>Resistance catalogue</td><td>{_safe_html(resist_cat or 'Not recorded — required')}</td><td>WHO/TBProfiler/Mykrobe</td></tr>
  <tr><td>Lineage-calling tool</td><td>{_safe_html(lineage_tool or 'Not recorded — required')}</td><td>—</td></tr>
  <tr><td>outbreaker2 version</td><td>{_safe_html(outbreaker_ver or 'Not recorded — required')}</td><td>—</td></tr>
  <tr><td>Generation time prior mean</td><td>Infectious to secondary case interval</td><td>{_safe_html(gen_time_mean or 'Not recorded')}</td></tr>
  <tr><td>Generation time prior SD</td><td></td><td>{_safe_html(gen_time_sd or 'Not recorded')}</td></tr>
  <tr><td>Sampling probability (π)</td><td>Proportion of cases sampled</td><td>{_safe_html(sampling_prob or 'Not recorded')}</td></tr>
  <tr><td>Random seed</td><td>Required for reproducibility</td><td>{_safe_html(random_seed or 'Not recorded — required')}</td></tr>
  <tr><td>MCMC iterations</td><td></td><td>{_safe_html(str((summary_data or {{}}).get('n_iter', (summary_data or {{}}).get('n_generations', 'n/a'))))}</td></tr>
  <tr><td>Burn-in</td><td></td><td>{_safe_html(str((summary_data or {{}}).get('burnin', 'n/a')))}</td></tr>
  <tr><td>Posterior samples</td><td></td><td>{_safe_html(str((summary_data or {{}}).get('n_samples', 'n/a')))}</td></tr>
</tbody></table></div>
<div class="callout callout-warn" style="margin-top:.5rem">
  Fields showing &ldquo;Not recorded&rdquo; must be populated in the <code>sequencing_runs</code>
  or <code>analysis_provenance</code> database tables before external circulation.
  Contact the bioinformatics lead to confirm the pipeline version and parameters used for this extract.
</div>"""

    # Transmission adjudication table — upgrade with final classification column
    def _adjudication_table(records, title):
        if not records:
            return ""
        rows = ""
        for item in records:
            post = float(item["posterior"])
            flag = item["validation_flag"]
            snp  = str(item["pairwise"])
            qc   = item["qc"]
            # Derive final classification
            if flag == "SNP-linked":
                final = "<span class='badge badge-green'>Genomically supported — escalate with epi</span>"
            elif flag == "QC-unresolved":
                final = "<span class='badge badge-red'>Hold — repeat sequencing required</span>"
            elif flag == "D1: SNP>12":
                final = "<span class='badge badge-orange'>Do not escalate — SNP discordant</span>"
            else:
                final = "<span class='badge badge-amber'>Model hypothesis — epi corroboration required</span>"
            rows += (f"<tr><td class='mono'>{_safe_html(item['pair'])}</td>"
                     f"<td>{_safe_html(f'{post:.3f}')}</td>"
                     f"<td>{_safe_html(snp)}</td>"
                     f"<td>{_safe_html(qc)}</td>"
                     f"<td>{_badge(flag)}</td>"
                     f"<td><em class='muted'>Awaiting epi review</em></td>"
                     f"<td>{final}</td></tr>")
        return (f"<h4>{_safe_html(title)}</h4>"
                f"<div class='tbl-wrap'><table><thead><tr>"
                f"<th>Pair</th><th>Posterior</th><th>SNP dist</th>"
                f"<th>QC src/rec</th><th>Genomic flag</th><th>Epidemiological link</th><th>Final classification</th>"
                f"</tr></thead><tbody>{rows}</tbody></table></div>")

    # PH interpretation statement
    snp_supported = len(genomic_pairs)
    model_only_ct = len(model_only_pairs)
    qc_unresolved_ct = len(qc_resolution_pairs)
    discordant_ct = len(genomically_discordant)

    if snp_supported > 0:
        ph_evidence_stmt = (
            f"There are <strong>{snp_supported}</strong> genomically-supported transmission pair(s) "
            f"(posterior ≥0.70 and SNP distance ≤12). These represent the highest-priority candidates "
            f"for operational action, but epidemiological corroboration is still required before field escalation."
        )
    else:
        ph_evidence_stmt = (
            "<strong>At present, there are no SNP-supported direct transmission links</strong> "
            "(no pairs meeting both posterior ≥0.70 and SNP distance ≤12 criteria). "
            "The outbreaker2 output identifies model-prioritised transmission hypotheses only."
        )

    ph_interpretation_html = f"""
<div class="callout" style="font-size:.92rem;line-height:1.65">
  <p style="margin-bottom:.5rem">{ph_evidence_stmt}</p>
  <p style="margin-bottom:.5rem">
    There are <strong>{_safe_html(str(model_only_ct))}</strong> model-only link(s) (no pairwise SNP confirmation),
    <strong>{_safe_html(str(qc_unresolved_ct))}</strong> QC-unresolved pair(s) pending repeat sequencing, and
    <strong>{_safe_html(str(discordant_ct))}</strong> genomically discordant pair(s) (model-linked but SNP &gt;12).
  </p>
  <p><strong>Operational action should focus on:</strong>
    (1) resolving QC failures and contamination flags,
    (2) validating drug-resistance gene-drug mapping and confirming phenotypic DST,
    (3) completing epidemiological linkage data for {_safe_html(str(int(open_clusters)))} open cluster(s),
    (4) populating all {_safe_html(str(len(missing_repro)))} missing reproducibility field(s) before external circulation{' — <strong>circulation is currently blocked</strong>' if missing_repro else ''}.
  </p>
  <p class="muted" style="margin-top:.4rem">This statement is automatically generated from available data.
  It must be reviewed and countersigned by the responsible public health physician before inclusion in any formal outbreak report.</p>
</div>"""

    # ─────────────────────────────────────────────────────────────────────────
    # Build HTML sections
    # ─────────────────────────────────────────────────────────────────────────

    # 1. Status banner
    if missing_repro:
        status_html = f'<span class="status-banner status-draft">DRAFT — {len(missing_repro)} reproducibility field(s) missing</span>'
    else:
        status_html = '<span class="status-banner status-ready">Governance gate passed — eligible for circulation</span>'

    # 2. Dashboard cards
    dashboard_html = f"""
<div class="metrics-grid">
  {_metric_card("Total cases", str(summary_total), f"{summary_sequenced} sequenced")}
  {_metric_card("Sequencing coverage", summary_coverage, "target ≥80%", alert=seq_pct is not None and float(seq_pct) < 80)}
  {_metric_card("QC pass rate", summary_qc_pass, "target ≥90%", alert=qc_pass_pct is not None and float(qc_pass_pct) < 90)}
  {_metric_card("QC unresolved", str(qc_status_counts['fail'] + qc_status_counts['not_reported'] + qc_status_counts['contamination']), f"{qc_status_counts['pass']} passed", alert=(qc_status_counts['fail'] + qc_status_counts['contamination']) > 0)}
  {_metric_card("Open clusters", str(open_clusters), f"{high_priority_open} priority >10")}
  {_metric_card("Model links ≥0.70", str(high_confidence_all_count), "Posterior ≥0.70 — validate with SNP+epi")}
  {_metric_card("SNP links ≤12", str(pairwise_links_le_12), "Direct transmission candidates")}
  {_metric_card("Model reliability", model_reliability, "MCMC convergence", alert=model_reliability=="Exploratory")}
</div>"""

    # Progress bars for coverage/QC
    seq_pct_bar = _progress(seq_pct, "Sequencing coverage %")
    qc_bar = _progress(qc_pass_pct, "QC pass rate %")

    # 3. Top Actions Due Now table — with status, team, dates, escalation trigger
    top_actions_html = """
<div class="tbl-wrap"><table>
<thead><tr><th>#</th><th>Action</th><th>Responsible team</th><th>Due</th><th>Status</th><th>Date raised</th><th>Escalation trigger</th></tr></thead>
<tbody>
<tr><td>1</td><td>Repeat sequencing / QC review for all failed or contaminated samples</td><td>Laboratory / Bioinformatics</td><td>48 h</td><td><span class="badge badge-red">Open</span></td><td>{gen_at}</td><td>If repeat fails again: exclude from cluster; flag to MDT</td></tr>
<tr><td>2</td><td>Validate drug-resistance pipeline gene-drug mapping; suppress unusual mappings from operational reports</td><td>Bioinformatics / Microbiology</td><td>Immediate</td><td><span class="badge badge-red">Open</span></td><td>{gen_at}</td><td>If validation fails: quarantine resistance calls until pipeline fix confirmed</td></tr>
<tr><td>3</td><td>Confirm phenotypic DST for all genomic resistance signals before clinical use</td><td>TB Microbiology / MDT</td><td>Immediate</td><td><span class="badge badge-red">Open</span></td><td>{gen_at}</td><td>If DST unavailable: treat as MDR pending result; notify clinician</td></tr>
<tr><td>4</td><td>Complete epidemiological data for all open clusters (demographics, setting, contacts)</td><td>TB Nurses / HPT / PHA</td><td>Next MDT</td><td><span class="badge badge-amber">In progress</span></td><td>{gen_at}</td><td>If epi incomplete at MDT: defer cluster closure; document gap</td></tr>
<tr><td>5</td><td>Do not escalate model-only links to field investigation without SNP ≤12 + epi corroboration</td><td>HPT / TB Nurses / MDT</td><td>Ongoing</td><td><span class="badge badge-amber">Standing</span></td><td>{gen_at}</td><td>If field escalation requested: require written MDT decision and documented epi rationale</td></tr>
<tr><td>6</td><td>Populate missing reproducibility metadata before external circulation</td><td>Bioinformatics / Lab Director</td><td>Before circulation</td><td><span class="badge badge-red">Open</span></td><td>{gen_at}</td><td>Block all external distribution until all 6 required fields are populated</td></tr>
<tr><td>7</td><td>MDT sign-off: document accepted/rejected/deferred for each open cluster</td><td>MDT Chair / PHA</td><td>Next MDT</td><td><span class="badge badge-amber">Pending</span></td><td>{gen_at}</td><td>If MDT not convened within 10 working days: escalate to programme lead</td></tr>
</tbody></table></div>""".format(gen_at=generated_at)

    # 4. MDT Governance table
    mdt_rows = [
        ("Circulation readiness", "Governance ready" if circulation_ok else "BLOCKED", "Complete mandatory reproducibility metadata before external circulation"),
        ("Model reliability", model_reliability, "Treat directionality as exploratory; diagnostics may be unavailable"),
        ("Open clusters", f"{int(open_clusters)} total / {high_priority_open} priority >10", "MDT review and epi data completion for all open clusters"),
        ("Discordant model links", f"{len(discordant_pairs)} identified", "Pairwise SNP + epi adjudication required"),
        ("MDT sign-off status", "Pending — MDT review required", "Chair to record: accepted / rejected / deferred for each open cluster"),
        ("Decision log", "Not yet completed", "Document MDT decisions in case management system; date-stamp and countersign"),
        ("QC failures", f"{qc_status_counts['fail']} fail / {qc_status_counts['contamination']} contamination", "Resolve before cluster assignment and model inference"),
    ]
    mdt_table_body = "".join(f"<tr><td>{_safe_html(a)}</td><td>{_safe_html(b)}</td><td>{_safe_html(c)}</td></tr>" for a, b, c in mdt_rows)
    mdt_html = f"""<div class="tbl-wrap"><table><thead><tr><th>Priority area</th><th>Current signal</th><th>Required MDT action</th></tr></thead>
<tbody>{mdt_table_body}</tbody></table></div>"""

    # 5. KPI table with progress bars
    kpi_kv = ""
    if isinstance(kpi_data, dict):
        kpi_fields = [
            ("Eligible cases (12 wks)", str(kpi_data.get("eligible_cases", "n/a"))),
            ("Sequenced cases", str(kpi_data.get("sequenced_cases", "n/a"))),
            ("Sequencing coverage", seq_pct_bar),
            ("QC reported cases", str(kpi_data.get("qc_reported_cases", "n/a"))),
            ("QC pass cases", str(kpi_data.get("qc_pass_cases", "n/a"))),
            ("QC fail cases", str(kpi_data.get("qc_fail_cases", "n/a"))),
            ("QC pass rate", qc_bar),
            ("Contamination flags", str(kpi_data.get("contamination_flag_cases", "n/a"))),
            ("Median days specimen→QC", str(kpi_data.get("median_days_specimen_to_qc", "n/a"))),
        ]
        if kpi_data.get("warning"):
            kpi_kv += f'<p class="muted">Warning: {_safe_html(str(kpi_data["warning"]))}</p>'
        kpi_kv += '<table class="kv-table"><tbody>' + "".join(
            f"<tr><th>{_safe_html(k)}</th><td>{v}</td></tr>" for k, v in kpi_fields
        ) + "</tbody></table>"

    # Regional representativeness
    region_rows_html = ""
    for row in ((kpi_data or {}).get("representativeness_by_region") or [])[:10]:
        region_rows_html += (f"<tr><td>{_safe_html(str(row.get('region','Unknown')))}</td>"
                             f"<td>{_safe_html(str(row.get('eligible_cases',0)))}</td>"
                             f"<td>{_safe_html(str(row.get('sequenced_cases',0)))}</td>"
                             f"<td>{_progress(row.get('sequenced_pct'))}</td></tr>")
    region_html = ""
    if region_rows_html:
        region_html = (f"<h3>Regional sequencing representativeness</h3>"
                       f"<div class='tbl-wrap'><table><thead><tr><th>Region</th><th>Eligible</th><th>Sequenced</th><th>Coverage</th></tr></thead>"
                       f"<tbody>{region_rows_html}</tbody></table></div>")

    # 6. Weekly trends table
    trend_rows_html = ""
    for r in weekly_trends:
        trend_rows_html += (f"<tr><td>{_safe_html(str(r.get('week_start','')))}</td>"
                            f"<td>{_safe_html(str(r.get('eligible',r.get('eligible_cases',0))))}</td>"
                            f"<td>{_safe_html(str(r.get('sequenced',r.get('sequenced_cases',0))))}</td>"
                            f"<td>{_progress(r.get('seq_pct',r.get('sequenced_pct')))}</td>"
                            f"<td>{_progress(r.get('qc_pct',r.get('qc_pass_pct')))}</td></tr>")
    weekly_html = ""
    if trend_rows_html:
        weekly_html = (f"<div class='tbl-wrap'><table><thead><tr>"
                       f"<th>Week</th><th>Eligible</th><th>Sequenced</th><th>Coverage %</th><th>QC pass %</th>"
                       f"</tr></thead><tbody>{trend_rows_html}</tbody></table></div>")
    else:
        weekly_html = '<p class="muted">Weekly trend data unavailable.</p>'

    # 7. Analysis summary table
    analysis_keys = [
        ("Posterior samples", "n_samples"), ("MCMC iterations", "n_iter"), ("MCMC iterations", "n_generations"),
        ("Burn-in", "burnin"), ("Mean log-likelihood", "likelihood_mean"), ("Likelihood SD", "likelihood_sd"),
        ("Transmission probability", "transmission_probability"), ("Generation time mean (days)", "generation_time_mean"),
        ("Generation time SD (days)", "generation_time_sd"), ("Sampling probability", "sampling_probability"),
        ("Convergence diagnostic", "convergence_diagnostic"),
    ]
    analysis_html = ""
    if isinstance(summary_data, dict):
        rows_out = ""
        for label, key in analysis_keys:
            if key in summary_data:
                v = summary_data[key]
                fv = f"{float(v):.3f}" if isinstance(v, float) and abs(float(v)) < 10 else (f"{float(v):.2f}" if isinstance(v, float) else str(v))
                rows_out += f"<tr><th>{_safe_html(label)}</th><td>{_safe_html(fv)}</td></tr>"
        if rows_out:
            analysis_html = f"<table class='kv-table'><tbody>{rows_out}</tbody></table>"
    if not analysis_html:
        analysis_html = '<p class="muted">No outbreaker2 analysis artifact found.</p>'

    # 8. Cluster prioritisation table
    cluster_pri_html = ""
    if cluster_action_rows:
        cluster_pri_rows = "".join(
            f"<tr><td class='mono'>{_safe_html(str(r.get('cluster_id',''))[:12])}</td>"
            f"<td>{_safe_html(str(r.get('case_count',0)))}</td>"
            f"<td>{_safe_html(str(r.get('region_count',0)))}</td>"
            f"<td>{_safe_html(str(r.get('most_recent_specimen','n/a')))}</td>"
            f"<td>{_safe_html(str(r.get('investigation_status','unknown')))}</td>"
            f"<td><strong>{_safe_html(str(r.get('priority_score',0)))}</strong></td></tr>"
            for r in cluster_action_rows
        )
        cluster_pri_html = (f"<div class='tbl-wrap'><table><thead><tr>"
                            f"<th>Cluster</th><th>Cases</th><th>Regions</th><th>Most recent</th><th>Status</th><th>Priority score</th>"
                            f"</tr></thead><tbody>{cluster_pri_rows}</tbody></table></div>")
    else:
        cluster_pri_html = '<p class="muted">No cluster action data available.</p>'

    # 9. Lineage/DR summary
    interpreted = int(analysis_summary.get("interpreted_samples", 0) or 0)
    with_lineage = int(analysis_summary.get("samples_with_lineage", 0) or 0)
    with_resist = int(analysis_summary.get("samples_with_resistance_calls", 0) or 0)
    lin_cov = f"{(with_lineage/interpreted*100):.1f}%" if interpreted else "n/a"
    res_cov = f"{(with_resist/interpreted*100):.1f}%" if interpreted else "n/a"
    lin_kv_rows = [
        ("Interpreted samples", str(interpreted)),
        ("Samples with lineage", str(with_lineage)),
        ("Lineage coverage", lin_cov),
        ("Samples with resistance calls", str(with_resist)),
        ("Resistance coverage", res_cov),
        ("Any resistance signal", str(analysis_epi_summary.get("samples_with_any_resistance_signal", 0))),
        ("Rifampicin-resistant (suspected)", str(analysis_epi_summary.get("rifampicin_resistant_suspected", 0))),
        ("Isoniazid-resistant (suspected)", str(analysis_epi_summary.get("isoniazid_resistant_suspected", 0))),
        ("MDR (suspected)", str(analysis_epi_summary.get("mdr_suspected", 0))),
        ("FQ-resistant (suspected)", str(analysis_epi_summary.get("fluoroquinolone_resistant_suspected", 0))),
    ]
    lineage_table_html = "<table class='kv-table'><tbody>" + "".join(
        f"<tr><th>{_safe_html(k)}</th><td>{_safe_html(v)}</td></tr>" for k, v in lin_kv_rows
    ) + "</tbody></table>"
    top_lineages = analysis_epi_summary.get("top_lineages") or []
    if top_lineages:
        lineage_table_html += "<p style='margin-top:.5rem'><strong>Top lineages: </strong>" + ", ".join(
            f"{_safe_html(x.get('lineage','?'))}: {_safe_html(str(x.get('count',0)))}" for x in top_lineages[:5]
        ) + "</p>"

    # 10. QC drill-down table
    qc_detail_rows = [r for r in case_rows if str(r.get("qc_status") or "").lower() not in ("pass", "passed") or bool(r.get("contamination_flag"))]
    if not qc_detail_rows:
        qc_detail_rows = [r for r in case_rows if r.get("qc_status")][:15]
    qc_detail_html = ""
    if qc_detail_rows:
        qc_trows = ""
        for r in qc_detail_rows[:30]:
            cov_raw = r.get("coverage_breadth")
            dep_raw = r.get("mean_depth")
            cov = f"{float(cov_raw):.1f}" if cov_raw is not None else "n/a"
            dep = f"{float(dep_raw):.1f}" if dep_raw is not None else "n/a"
            contam = "yes" if bool(r.get("contamination_flag")) else "no"
            qc_st = str(r.get("qc_status") or "not_reported")
            repeat = "yes" if qc_st.lower() not in ("pass", "passed") or contam == "yes" else "no"
            row_class = ' style="background:var(--alert-bg)"' if repeat == "yes" else ""
            action_badge = "<span class='badge badge-red'>Repeat</span>" if repeat == "yes" else "<span class='badge badge-green'>OK</span>"
            qc_trows += (f"<tr{row_class}><td class='mono'>{_safe_html(_short_case_id(str(r.get('case_id',''))))}</td>"
                         f"<td>{_safe_html(qc_st)}</td><td>{_safe_html(cov)}</td><td>{_safe_html(dep)}</td>"
                         f"<td>{_safe_html(contam)}</td>"
                         f"<td>{action_badge}</td></tr>")
        qc_detail_html = (f"<div class='tbl-wrap'><table><thead><tr><th>Sample</th><th>QC status</th><th>Coverage %</th>"
                          f"<th>Mean depth</th><th>Contamination</th><th>Action</th></tr></thead><tbody>{qc_trows}</tbody></table></div>")
    else:
        qc_detail_html = '<p class="muted">No QC details available.</p>'

    # QC summary cards
    qc_summary_html = f"""
<div class="metrics-grid">
  {_metric_card("QC pass", str(qc_status_counts['pass']), f"{summary_qc_pass} pass rate")}
  {_metric_card("QC fail", str(qc_status_counts['fail']), "Low coverage / threshold breach", alert=qc_status_counts['fail']>0)}
  {_metric_card("Contamination", str(qc_status_counts['contamination']), "Mixed signal — exclude pending repeat", alert=qc_status_counts['contamination']>0)}
  {_metric_card("Not reported", str(qc_status_counts['not_reported']), "QC metadata absent — treat as unresolved")}
  {_metric_card("Excluded from inference", str(excluded_from_outbreaker), "QC fail or contamination", alert=excluded_from_outbreaker>0)}
</div>"""

    # 11. Run-level QC table
    run_qc_html = ""
    if run_qc_rows_db:
        run_rows_out = ""
        for rl in run_qc_rows_db:
            total = int(rl.get("total_samples") or 0)
            fail = int(rl.get("fail_count") or 0)
            fail_pct = f"{round(100*fail/total,1)}%" if total else "n/a"
            alert_style = ' style="background:var(--alert-bg)"' if total and (fail/total) > 0.2 else ""
            run_rows_out += (f"<tr{alert_style}><td>{_safe_html(str(rl.get('run_date','n/a')))}</td>"
                             f"<td>{_safe_html(str(total))}</td><td>{_safe_html(str(int(rl.get('pass_count',0))))}</td>"
                             f"<td>{_safe_html(str(fail))}</td><td>{_safe_html(fail_pct)}</td>"
                             f"<td>{_safe_html(str(rl.get('mean_depth_avg','n/a')))}</td>"
                             f"<td>{_safe_html(str(rl.get('mean_coverage_avg','n/a')))}</td></tr>")
        run_qc_html = (f"<div class='tbl-wrap'><table><thead><tr><th>Run date (proxy)</th><th>Samples</th>"
                       f"<th>Pass</th><th>Fail</th><th>Fail %</th><th>Mean depth</th><th>Mean coverage %</th>"
                       f"</tr></thead><tbody>{run_rows_out}</tbody></table></div>"
                       "<p class='muted'>Rows highlighted red have fail rate &gt;20%. Assign run_id to enable full run-level audit.</p>")
    else:
        run_qc_html = '<p class="muted">Run-level QC unavailable — reported_at or run_id not recorded.</p>'

    # 12. Drug-resistance mutation table
    mut_rows_html = ""
    mut_count = 0
    for base in mutation_rows_raw:
        case_id_mut = str(base.get("case_id") or "")
        for mut in _iter_resistance_mutations(base.get("resistance_mutations")):
            drug = str(mut.get("drug") or "n/a")
            gene = str(mut.get("gene") or "n/a")
            validity = _drug_gene_status_label(drug, gene)
            pred_text = _resistance_profile_text(base.get("predicted_drug_resistance"))
            badge_class = "badge-green" if "Valid" in validity else ("badge-red" if "Unusual" in validity else "badge-grey")
            mut_rows_html += (f"<tr><td class='mono'>{_safe_html(_short_case_id(case_id_mut))}</td>"
                              f"<td>{_safe_html(drug)}</td><td class='mono'>{_safe_html(str(mut.get('mutation','n/a')))}</td>"
                              f"<td class='mono'>{_safe_html(gene)}</td>"
                              f"<td><span class='badge {badge_class}'>{_safe_html(validity)}</span></td>"
                              f"<td>{_safe_html(str(mut.get('confidence','n/a')))}</td>"
                              f"<td>{_safe_html(pred_text)}</td></tr>")
            mut_count += 1
            if mut_count >= 60:
                break
        if mut_count >= 60:
            break
    dr_table_html = ""
    if mut_rows_html:
        dr_table_html = (f"<div class='tbl-wrap'><table><thead><tr><th>Case</th><th>Drug</th><th>Mutation</th>"
                         f"<th>Gene</th><th>Gene-drug status</th><th>Confidence</th><th>Predicted profile</th>"
                         f"</tr></thead><tbody>{mut_rows_html}</tbody></table></div>")
    else:
        dr_table_html = '<p class="muted">No structured resistance-mutation details found.</p>'

    # 13. Cluster epidemiology tables
    cluster_epi_html = ""
    if cluster_epi_rows:
        cepi_rows = ""
        for r in cluster_epi_rows:
            cid = _short_case_id(str(r.get("cluster_id") or ""))
            rr_mdr = f"{int(r.get('rr_cases') or 0)}/{int(r.get('mdr_cases') or 0)}"
            recent = f"{int(r.get('recent_30d') or 0)}/{int(r.get('recent_60d') or 0)}/{int(r.get('recent_90d') or 0)}"
            cepi_rows += (f"<tr><td class='mono'>{_safe_html(cid)}</td><td>{_safe_html(str(r.get('cases',0)))}</td>"
                          f"<td>{_safe_html(str(r.get('first_specimen','n/a')))}</td><td>{_safe_html(str(r.get('latest_specimen','n/a')))}</td>"
                          f"<td>{_safe_html(str(r.get('median_snp_proxy','n/a')))}</td><td>{_safe_html(str(r.get('max_snp_proxy','n/a')))}</td>"
                          f"<td>{_safe_html(rr_mdr)}</td><td class='mono'>{_safe_html(_short_case_id(str(r.get('suspected_index_case','n/a'))))}</td>"
                          f"<td>{_safe_html(recent)}</td></tr>")
        cluster_epi_html = (f"<div class='tbl-wrap'><table><thead><tr><th>Cluster</th><th>Cases</th><th>First specimen</th>"
                            f"<th>Latest specimen</th><th>Median SNP</th><th>Max SNP</th><th>RR/MDR cases</th><th>Index case</th><th>Recent 30/60/90d</th>"
                            f"</tr></thead><tbody>{cepi_rows}</tbody></table></div>")
        # Growth status
        growth_rows = ""
        for r in cluster_epi_rows:
            cid = _short_case_id(str(r.get("cluster_id") or ""))
            latest_raw = r.get("latest_specimen")
            if latest_raw is None:
                latest_str = "n/a"
                days_since = None
            elif hasattr(latest_raw, "isoformat"):
                latest_str = latest_raw.isoformat()[:10]
                try:
                    days_since = (today - (latest_raw.date() if hasattr(latest_raw, "date") else latest_raw)).days
                except Exception:
                    days_since = None
            else:
                latest_str = str(latest_raw)[:10]
                try:
                    days_since = (today - _dt.date.fromisoformat(latest_str)).days
                except Exception:
                    days_since = None
            if days_since is None:
                status = "Unknown"
                badge = "badge-grey"
            elif days_since < 90:
                status = "Active"
                badge = "badge-red"
            elif days_since < 180:
                status = "Slowing"
                badge = "badge-amber"
            else:
                status = "Likely inactive"
                badge = "badge-green"
            growth_rows += (f"<tr><td class='mono'>{_safe_html(cid)}</td><td>{_safe_html(str(r.get('cases',0)))}</td>"
                            f"<td>{_safe_html(latest_str)}</td><td>{_safe_html(str(r.get('recent_30d',0)))}</td>"
                            f"<td>{_safe_html(str(r.get('recent_60d',0)))}</td><td>{_safe_html(str(r.get('recent_90d',0)))}</td>"
                            f"<td><span class='badge {badge}'>{_safe_html(status)}</span></td></tr>")
        cluster_epi_html += (f"<h3>Cluster growth status</h3>"
                             f"<div class='tbl-wrap'><table><thead><tr><th>Cluster</th><th>Cases</th><th>Last case</th>"
                             f"<th>Cases 30d</th><th>Cases 60d</th><th>Cases 90d</th><th>Growth status</th>"
                             f"</tr></thead><tbody>{growth_rows}</tbody></table></div>"
                             "<p class='muted'>Active = last case &lt;90 days; Slowing = 90–180 days; Likely inactive = &gt;180 days. Formal closure requires MDT sign-off.</p>")
    else:
        cluster_epi_html = '<p class="muted">No cluster epidemiology data available.</p>'

    # 14. Method comparison
    comp_html = ""
    if isinstance(method_comparison_data, dict):
        coverage = method_comparison_data.get("coverage") or {}
        agreement = method_comparison_data.get("agreement") or {}
        comp_kv = [
            ("Sequence assigned cases", str(coverage.get("sequence_assigned_cases", 0))),
            ("Outbreaker assigned cases", str(coverage.get("outbreaker_assigned_cases", 0))),
            ("Overlap cases", str(coverage.get("overlap_cases", 0))),
            ("Pairwise precision", str(round(float(agreement.get("pairwise_precision_outbreaker_vs_sequence", 0.0)), 3))),
            ("Pairwise recall", str(round(float(agreement.get("pairwise_recall_outbreaker_vs_sequence", 0.0)), 3))),
            ("Pairwise Jaccard", str(round(float(agreement.get("pairwise_jaccard", 0.0)), 3))),
        ]
        comp_html = "<table class='kv-table'><tbody>" + "".join(f"<tr><th>{_safe_html(k)}</th><td>{_safe_html(v)}</td></tr>" for k, v in comp_kv) + "</tbody></table>"
    else:
        comp_html = '<p class="muted">No cluster method comparison artifact found.</p>'

    # 15. Sequence clustering snapshot
    seq_cluster_html = ""
    if isinstance(sequence_summary_data, dict):
        seq_kv = [(k.replace("_", " ").title(), str(sequence_summary_data[k])) for k in ["total_sequences", "assigned_sequences", "cluster_count", "largest_cluster_size", "singleton_count"] if k in sequence_summary_data]
        seq_cluster_html = "<table class='kv-table'><tbody>" + "".join(f"<tr><th>{_safe_html(k)}</th><td>{_safe_html(v)}</td></tr>" for k, v in seq_kv) + "</tbody></table>"
    else:
        seq_cluster_html = '<p class="muted">No sequence clustering summary artifact.</p>'

    # 16. Case-level actions (from CSV)
    action_limit_label = "all rows" if full else "first 25 rows"
    actions_csv_html = _data_table_html(
        action_rows_csv,
        [("Case", "Case"), ("Cluster", "Cluster"), ("Pairwise SNP?", "Pairwise SNP?"),
         ("NN SNP", "NN SNP"), ("Likely link", "Likely link"), ("Posterior", "Posterior"),
         ("Tier", "Tier"), ("QC", "QC"), ("Recommended action", "Recommended action")],
        "No case-level action export found. Generate the PDF report first to populate exports/appendix_a_case_level_actions.csv."
    )
    discordance_csv_html = _data_table_html(
        discordance_rows_csv,
        [("Case pair", "Case Pair"), ("Pairwise SNP result", "Pairwise SNP result"),
         ("Outbreaker2", "Outbreaker2"), ("Pairwise SNP", "Pairwise SNP"),
         ("Posterior", "Posterior"), ("Code", "Code"), ("Interpretation", "Interpretation")],
        "No discordance review export found."
    )

    # 17. Transmission network section
    key_nodes_html = _data_table_html(
        key_nodes[:15 if not full else None],
        [("Case", "case_id"), ("Cluster", "cluster_id"), ("Region", "region"),
         ("Risk score", "risk_score"), ("Risk band", "risk_band"),
         ("Outgoing", "outgoing_links"), ("Incoming", "incoming_links")],
        "No priority-node data."
    )
    network_edges_html = _data_table_html(
        network_edges[:15 if not full else None],
        [("From", "source"), ("To", "target"), ("Probability", "probability"),
         ("Confidence", "confidence"), ("Inference", "inference")],
        "No transmission-link data."
    )
    network_meta_html = ""
    if isinstance(transmission_data, dict):
        net_kv = [(l, k) for l, k in [("Generated at", "generated_at"), ("Inference source", "inference_source"),
                   ("Provenance", "provenance"), ("Node count", "node_count"), ("Edge count", "edge_count"),
                   ("High-confidence edges", "high_confidence_edges")] if transmission_data.get(k) is not None]
        network_meta_html = "<table class='kv-table'><tbody>" + "".join(
            f"<tr><th>{_safe_html(l)}</th><td>{_safe_html(str(transmission_data.get(k)))}</td></tr>" for l, k in net_kv
        ) + "</tbody></table>"

    # 18. Discordant pair summary table (from computed data)
    disc_computed_html = ""
    if discordant_pairs:
        def _fmt_disc(d):
            _pairwise_str = str(d['pairwise']) if d['pairwise'] is not None else 'n/a'
            _post_str = f"{d['posterior']:.3f}" if d['posterior'] else 'n/a'
            return (f"<tr><td class='mono'>{_safe_html(d['pair'])}</td>"
                    f"<td>{_safe_html(_pairwise_str)}</td>"
                    f"<td>{_safe_html(_post_str)}</td>"
                    f"<td><span class='badge badge-orange'>{_safe_html(d['disc_code'])}</span></td>"
                    f"<td>{_safe_html(d['interpretation'])}</td></tr>")
        disc_rows_out = "".join(
            _fmt_disc(d)
            for d in sorted(discordant_pairs, key=lambda x: x["posterior"], reverse=True)[:40 if full else 10]
        )
        disc_computed_html = (f"<div class='tbl-wrap'><table><thead><tr>"
                              f"<th>Pair</th><th>SNP dist</th><th>Posterior</th><th>Code</th><th>Interpretation</th>"
                              f"</tr></thead><tbody>{disc_rows_out}</tbody></table></div>"
                              "<p class='muted'>D1: outbreaker-linked, SNP&gt;12. D2: outbreaker-linked, SNP unavailable. D3: SNP-linked, no outbreaker direction.</p>")
    else:
        disc_computed_html = '<p class="muted">No discordant pairs identified from available outputs.</p>'

    # 19. Data provenance section — full extended table
    _missing_badge = "<span class='badge badge-red'>Missing &#8212; required</span>"
    _optional_badge = "<span class='badge badge-grey'>Not recorded</span>"

    def _prov_row(lbl, v, required=True):
        is_missing = v is None or (isinstance(v, str) and not v.strip())
        row_style = ' style="background:var(--alert-bg)"' if (is_missing and required) else ""
        cell = (_missing_badge if required else _optional_badge) if is_missing else _safe_html(str(v))
        return f"<tr{row_style}><th>{_safe_html(lbl)}</th><td>{cell}</td></tr>"

    prov_rows = "".join(_prov_row(lbl, v, required=True) for lbl, v in required_repro_metadata)
    # Extended optional fields
    extended_prov = [
        ("Sequencing platform",       seq_platform,   False),
        ("Instrument",                instrument,     False),
        ("Library prep method",       library_prep,   False),
        ("Mapping tool",              mapping_tool,   False),
        ("Variant caller",            variant_caller, False),
        ("SNP cluster threshold",     snp_threshold,  False),
        ("Generation time mean (d)",  gen_time_mean,  False),
        ("Generation time SD (d)",    gen_time_sd,    False),
        ("Sampling probability (π)",  sampling_prob,  False),
        ("Pipeline run date",         str(_seq_run_row.get("completed_at", "") or _prov_row_db.get("run_at", "") or "") or None, False),
        ("Analysis provenance date",  str(_prov_row_db.get("generated_at", "") or _prov_row_db.get("run_at", "") or "") or None, False),
    ]
    prov_rows += "".join(_prov_row(lbl, v, required=req) for lbl, v, req in extended_prov)
    prov_html = f"<table class='kv-table'><tbody>{prov_rows}</tbody></table>"

    # 20. Key concepts reference
    concepts = [
        ("Whole-Genome Sequencing (WGS)", "Reads the complete ~4.4 Mb genome of M. tuberculosis. More informative than conventional typing (MIRU, spoligotyping)."),
        ("SNP", "A single base-pair difference. Closely related strains share few SNPs. Used as a genetic distance metric."),
        ("SNP threshold for transmission", "≤12 SNPs: potentially linked (UK NICE). ≤5 SNPs: recent direct transmission likely. >50 SNPs: recent shared transmission effectively ruled out."),
        ("Lineage", "M. tuberculosis classified into 7+ major lineages (L1–L7). Influences drug-resistance patterns and transmissibility."),
        ("Cluster", "Cases genetically similar within the SNP threshold. Does not prove direct transmission — epidemiological linkage required to confirm routes."),
        ("outbreaker2", "Bayesian MCMC method combining SNP distances with collection dates to probabilistically infer who-infected-whom. Posterior probabilities are hypotheses, not proofs."),
        ("MCMC convergence", "Convergence diagnostic near 1.0 = reliable. Values >1.1 = interpret cautiously."),
        ("Drug resistance", "Genomic mutations predict resistance. MDR-TB = resistant to isoniazid + rifampicin. XDR-TB = additional resistance. Genomic DR requires phenotypic DST confirmation."),
    ]
    concepts_html = "".join(
        f"<details><summary>{_safe_html(term)}</summary><div><p>{_safe_html(defn)}</p></div></details>"
        for term, defn in concepts
    )

    # 21. Raw artifacts section (full only)
    raw_artifacts_html = ""
    if full:
        raw_artifacts_html = f"""
<div class="card" id="raw-artifacts">
  <h2>Full machine-readable artifacts</h2>
  <p class="muted">Complete JSON exports used to produce this report.</p>
  <details><summary>Outbreaker2 summary artifact</summary><div><pre>{_safe_html(json.dumps(summary_data, indent=2, default=str) if summary_data else 'No artifact found.')}</pre></div></details>
  <details><summary>Transmission network artifact</summary><div><pre>{_safe_html(json.dumps(transmission_data, indent=2, default=str) if transmission_data else 'No artifact found.')}</pre></div></details>
  <details><summary>Sequence clustering summary</summary><div><pre>{_safe_html(json.dumps(sequence_summary_data, indent=2, default=str) if sequence_summary_data else 'No artifact found.')}</pre></div></details>
  <details><summary>Lineage/DR validation</summary><div><pre>{_safe_html(json.dumps(lineage_dr_data, indent=2, default=str) if lineage_dr_data else 'No artifact found.')}</pre></div></details>
</div>"""

    # ─────────────────────────────────────────────────────────────────────────
    # Assemble final HTML
    # ─────────────────────────────────────────────────────────────────────────

    # Build the improved pairs section using adjudication table
    adj_genomic_html    = _adjudication_table(genomic_pairs[:20 if not full else None],    "Genomically supported (SNP ≤12, shared cluster)")
    adj_model_html      = _adjudication_table(model_only_pairs[:20 if not full else None],  "Model-only — no pairwise SNP data")
    adj_discordant_html = _adjudication_table(genomically_discordant[:20 if not full else None], "Genomically discordant (posterior ≥0.70, SNP >12)")
    adj_qcunres_html    = _adjudication_table(qc_resolution_pairs[:20 if not full else None], "QC-unresolved — hold pending repeat sequencing")

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Outbreak Investigation Report — NI TB Genomic Surveillance</title>
  <style>{css}</style>
</head>
<body>
<header>
  <h1>NI TB Genomic Surveillance</h1>
  <p>Outbreak Investigation Report &mdash; generated {_safe_html(generated_at)} &nbsp;&middot;&nbsp; {_safe_html(report_label)}</p>
  {status_html}
</header>

<div class="layout">
  <!-- ── Sticky sidebar nav ── -->
  <nav class="sidebar" aria-label="Report sections">
    <h3>Overview</h3>
    <a href="#executive">Executive summary</a>
    <a href="#denominators">Denominators</a>
    <a href="#actions-now">Top actions due now</a>
    <a href="#mdt">MDT governance</a>
    <h3>Analysis</h3>
    <a href="#analysis">outbreaker2 analysis</a>
    <a href="#transmission">Transmission network</a>
    <a href="#snp-summary">Pairwise SNP summary</a>
    <a href="#interpretation">Outbreak interpretation</a>
    <h3>Sequencing &amp; QC</h3>
    <a href="#kpis">Programme KPIs</a>
    <a href="#weekly">Weekly trends</a>
    <a href="#qc">QC drill-down</a>
    <a href="#run-qc">Run-level QC</a>
    <h3>Resistance</h3>
    <a href="#lineage">Lineage/DR summary</a>
    <a href="#mutations">Mutation details</a>
    <h3>Clusters</h3>
    <a href="#clusters">Cluster epidemiology</a>
    <a href="#cluster-pri">Cluster prioritisation</a>
    <h3>Methods</h3>
    <a href="#methods">Methods &amp; pipeline</a>
    <a href="#provenance">Data provenance</a>
    <a href="#epi-completeness">Epi data completeness</a>
    <h3>Appendices</h3>
    <a href="#appendix-a">Appendix A — Case actions</a>
    <a href="#appendix-b">Appendix B — Discordance</a>
    <a href="#figures">Figures</a>
    <a href="#concepts">Key concepts</a>
    {'<a href="#raw-artifacts">Raw artifacts</a>' if full else ''}
  </nav>

  <main>
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- EXECUTIVE SUMMARY -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <section class="card" id="executive">
      <h2>Executive summary</h2>
      {dashboard_html}
      {'<div class="callout callout-alert"><strong>DRAFT REPORT:</strong> Missing reproducibility fields: ' + _safe_html(', '.join(missing_repro)) + '. External circulation is blocked until these are populated.</div>' if missing_repro else '<div class="callout"><strong>Governance gate passed.</strong> Reproducibility metadata is complete. Publish only after information-governance review.</div>'}
    </section>

    <!-- TOP ACTIONS -->
    <section class="card" id="actions-now">
      <h2>Top actions due now</h2>
      <div class="section-note">Items marked <strong>model-only</strong> or <strong>exploratory</strong> require genomic validation and epidemiological corroboration before operational action.</div>
      {top_actions_html}
    </section>

    <!-- MDT GOVERNANCE -->
    <section class="card" id="mdt">
      <h2>MDT governance summary</h2>
      {mdt_html}
      <p class="muted" style="margin-top:.5rem">Full MDT action sheet with final discordant-pair counts is in Appendix B.</p>
    </section>

    <!-- ABOUT -->
    <section class="card" id="about">
      <h2>About this report</h2>
      <p style="font-size:.87rem">This report is produced by the Northern Ireland TB Genomic Surveillance platform using whole-genome sequencing (WGS) data and epidemiological case records. It supports TB programme staff and public health investigators by providing genomic evidence for transmission clusters, drug-resistance profiles, and programme performance metrics.</p>
      <div class="callout-warn callout" style="margin-top:.6rem"><strong>Decision-support tool only.</strong> All findings must be reviewed and acted on by a qualified clinician or public health professional. No automated decisions are made.</div>
    </section>

    <!-- POPULATION AND DENOMINATORS -->
    <section class="card" id="denominators">
      <h2>Population and denominators</h2>
      <div class="section-note">Use this table to interpret all percentages in this report. Every metric is expressed relative to one of these denominator counts.</div>
      {denom_html}
    </section>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- ANALYSIS -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <section class="card" id="analysis">
      <h2>outbreaker2 analysis summary</h2>
      {analysis_html}
      <div class="section-note" style="margin-top:.7rem">
        <strong>How to interpret:</strong> Pairs with high posterior transmission probability are model-prioritised hypotheses only.
        They should not be interpreted as direct transmission unless supported by pairwise SNP distance ≤12, QC pass status, and epidemiological corroboration.
      </div>
      <h3>MCMC diagnostics</h3>
      <div class="figures-grid">
        {_figure_card("outbreaker_trace")}
        {_figure_card("outbreaker_hist")}
      </div>
    </section>

    <!-- TRANSMISSION NETWORK -->
    <section class="card" id="transmission">
      <h2>Transmission network</h2>
      {network_meta_html}
      <div class="fig-inline">{_figure_card("outbreaker_tree")}</div>
      <h3>Priority nodes</h3>
      {key_nodes_html}
      <h3>Transmission links</h3>
      {network_edges_html}
    </section>

    <!-- MODEL-PRIORITISED PAIRS — WITH ADJUDICATION TABLE -->
    <section class="card" id="pairs">
      <h2>Model-prioritised transmission hypotheses</h2>
      <div class="callout callout-alert">
        <strong>Do not escalate model-only or genomically discordant links to field investigation</strong> without genomic and epidemiological corroboration.
        Posterior probability &ge;0.70 indicates a plausible transmission event, but genomic validation is essential.
      </div>
      <div style="margin:.6rem 0">
        <span class="tag">{_safe_html(str(len(genomic_pairs)))} SNP-linked</span>
        <span class="tag">{_safe_html(str(len(model_only_pairs)))} Model-only</span>
        <span class="tag">{_safe_html(str(len(genomically_discordant)))} D1: SNP&gt;12</span>
        <span class="tag">{_safe_html(str(len(qc_resolution_pairs)))} QC-unresolved</span>
      </div>
      <div class="section-note">The <strong>Epidemiological link</strong> and <strong>Final classification</strong> columns below are pre-populated with default values. The MDT should review each pair and record the final adjudication decision in the case management system before external circulation.</div>
      {adj_genomic_html}
      {adj_model_html}
      {adj_discordant_html}
      {adj_qcunres_html}
      {('<p class="muted">No high-confidence (&ge;0.70) edges found in transmission network.</p>' if not high_confidence_edges else '')}
    </section>

    <!-- SNP SUMMARY -->
    <section class="card" id="snp-summary">
      <h2>Pairwise SNP distance summary</h2>
      <div class="section-note">
        ≤12 SNPs = operational threshold for probable recent transmission.
        &gt;12 SNPs = direct transmission unlikely.
        SNP unavailable = repeat sequencing required before inference.
      </div>
      <div class="tbl-wrap"><table><thead><tr><th>SNP distance category</th><th>Pairs</th><th>Operational implication</th></tr></thead><tbody>
        <tr><td>0–5 SNPs (direct)</td><td>{_safe_html(str(sum(1 for d in pairwise_snp_matrix.values() if d<=5)))}</td><td>Immediate: probable direct transmission — contact trace; confirm epi link</td></tr>
        <tr><td>6–12 SNPs (probable)</td><td>{_safe_html(str(sum(1 for d in pairwise_snp_matrix.values() if 6<=d<=12)))}</td><td>Priority: probable cluster; review shared setting and exposures</td></tr>
        <tr><td>13–25 SNPs (possible shared source)</td><td>{_safe_html(str(sum(1 for d in pairwise_snp_matrix.values() if 13<=d<=25)))}</td><td>Review: possible shared source/reactivation; epi adjudication required</td></tr>
        <tr><td>&gt;25 SNPs (unlikely direct)</td><td>{_safe_html(str(sum(1 for d in pairwise_snp_matrix.values() if d>25)))}</td><td>Low priority: unlikely direct recent transmission; monitor only</td></tr>
        <tr><td>SNP unavailable</td><td>{_safe_html(str(sum(1 for r in case_rows if not sequence_by_case.get(str(r.get('case_id',''))) and r.get('case_id'))))}</td><td>Hold: repeat sequencing or QC resolution required before inference</td></tr>
      </tbody></table></div>
    </section>

    <!-- OUTBREAK INTERPRETATION -->
    <section class="card" id="interpretation">
      <h2>Current outbreak interpretation</h2>
      {ph_interpretation_html}

      <h3>Counts and denominators in this report</h3>
      <div class="section-note">Definitions match the denominator box above. All model-prioritised links are hypotheses only — zero SNP-supported links means no validated direct transmission candidates at this time.</div>
      <div class="tbl-wrap"><table><thead><tr><th>Metric</th><th>Count</th><th>Definition</th></tr></thead><tbody>
        <tr><td>Model-prioritised links &ge;0.70</td><td>{_safe_html(str(high_confidence_all_count))}</td><td>All outbreaker2 edges with posterior probability &ge;0.70</td></tr>
        <tr><td>Discordant pairs reviewed</td><td>{_safe_html(str(len(discordant_pairs)))}</td><td>All model-linked pairs showing SNP/model discordance requiring adjudication</td></tr>
        <tr><td>Displayed network links</td><td>{_safe_html(str(high_confidence_snapshot_count))}</td><td>Links in network JSON snapshot (may be filtered for display)</td></tr>
      </tbody></table></div>

      <h3>Discordant pairs</h3>
      {disc_computed_html}
    </section>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- KPIs & TRENDS -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <section class="card" id="kpis">
      <h2>Programme surveillance KPIs (last 12 weeks)</h2>
      {kpi_kv}
      {region_html}
    </section>

    <section class="card" id="weekly">
      <h2>Weekly surveillance trends (12 weeks)</h2>
      {weekly_html}
    </section>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- QC -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <section class="card" id="qc">
      <h2>QC failure drill-down</h2>
      {qc_summary_html}
      <h3>QC pass/fail thresholds applied</h3>
      {qc_thresholds_html}
      <h3>Sample-level QC detail</h3>
      <div class="section-note" style="margin-top:.6rem">
        Samples highlighted red require repeat sequencing or resolution before operational inference.
        Contaminated samples must be excluded from cluster assignment pending repeat.
        The <strong>qc_failure_reason</strong> field (from sample_qc_metrics) is shown where available.
      </div>
      {qc_detail_html}
    </section>

    <section class="card" id="run-qc">
      <h2>Run-level QC summary</h2>
      <div class="section-note">Runs with fail rate &gt;20% or mean coverage &lt;95% should be reviewed before results are used for cluster assignment.</div>
      {run_qc_html}
    </section>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- LINEAGE / DR -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <section class="card" id="lineage">
      <h2>Lineage and drug-resistance summary</h2>
      {lineage_table_html}
      <div class="fig-inline" style="margin-top:.9rem">{_figure_card("outbreaker_resistance")}</div>
    </section>

    <section class="card" id="mutations">
      <h2>Drug-resistance mutation details</h2>
      <div class="callout callout-alert">
        <strong>CLINICAL SAFETY NOTICE:</strong> All genomic resistance predictions are <em>not for clinical use until phenotypic DST is confirmed</em>.
        Gene-drug combinations marked <span class="badge badge-red">Unusual</span> (e.g. gyrA linked to pyrazinamide) indicate a possible
        data-mapping error and <strong>must be suppressed from operational reports</strong> until the bioinformatics pipeline is validated.
        If unusual mappings are observed, quarantine the affected samples and notify the bioinformatics lead immediately.
        Catalogue version used for this analysis: <strong>{_safe_html(resist_cat or 'Not recorded — required')}</strong>.
      </div>
      <details open><summary>Gene-drug reference mapping</summary><div>
        <div class="tbl-wrap"><table><thead><tr><th>Drug</th><th>Expected genes</th></tr></thead><tbody>
          <tr><td>Rifampicin</td><td>rpoB</td></tr>
          <tr><td>Isoniazid</td><td>katG, inhA, fabG1</td></tr>
          <tr><td>Pyrazinamide</td><td>pncA</td></tr>
          <tr><td>Ethambutol</td><td>embB</td></tr>
          <tr><td>Fluoroquinolones</td><td>gyrA, gyrB</td></tr>
          <tr><td>Aminoglycosides / injectables</td><td>rrs, eis, tlyA</td></tr>
        </tbody></table></div>
      </div></details>
      {dr_table_html}
    </section>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- CLUSTERS -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <section class="card" id="clusters">
      <h2>Cluster epidemiology</h2>
      <p class="muted">Genomic summary + growth status for all active clusters. RR/MDR column = rifampicin-resistant / MDR-TB suspected cases.</p>
      <div class="fig-inline">{_figure_card("outbreaker_phylo")}</div>
      {cluster_epi_html}
    </section>

    <section class="card" id="cluster-pri">
      <h2>Cluster prioritisation</h2>
      <div class="section-note">Priority score = case count×2 + cross-region spread×3 + recency (14/30/60d = 3/2/1) + open status×3. Scores &gt;10 warrant prioritised MDT review.</div>
      {cluster_pri_html}
    </section>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- METHODS -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <section class="card" id="methods">
      <h2>Method comparison &amp; secondary engines</h2>
      <h3>Cross-method clustering comparison</h3>
      {comp_html}
      <h3>Sequence clustering snapshot</h3>
      {seq_cluster_html}
    </section>

    <!-- DATA PROVENANCE -->
    <section class="card" id="provenance">
      <h2>Data provenance and reproducibility</h2>
      <div class="section-note">Fields marked <span class="badge badge-red">Missing &#8212; required</span> are mandatory for external circulation. Fields marked <span class="badge badge-grey">Not recorded</span> are optional but strongly recommended for audit and reproducibility.</div>
      {prov_html}
      {'<div class="callout callout-alert" style="margin-top:.6rem"><strong>' + str(len(missing_repro)) + ' required field(s) missing.</strong> External circulation is blocked until all mandatory reproducibility fields are populated. Populate via <code>sequencing_runs</code> or <code>analysis_provenance</code> database tables.</div>' if missing_repro else '<div class="callout" style="margin-top:.6rem">All mandatory reproducibility fields are present. Confirm catalogue and tool versions with the bioinformatics lead before circulation.</div>'}
    </section>

    <!-- EPIDEMIOLOGICAL DATA COMPLETENESS -->
    <section class="card" id="epi-completeness">
      <h2>Epidemiological data completeness</h2>
      <div class="section-note">Completeness of fields capturable from the genomic pipeline. Additional clinical and field epi data must be completed in the case management system.</div>
      {epi_complete_html}
    </section>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- APPENDICES -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <section class="card" id="appendix-a">
      <h2>Appendix A — Case-level operational actions</h2>
      <details open><summary>Table A1 — Case actions ({_safe_html(action_limit_label)})</summary><div>
        {actions_csv_html}
      </div></details>
    </section>

    <section class="card" id="appendix-b">
      <h2>Appendix B — Full discordance review</h2>
      <details open><summary>Discordance table ({_safe_html(action_limit_label)})</summary><div>
        {discordance_csv_html}
      </div></details>
    </section>

    <!-- FIGURES -->
    <section class="card" id="figures">
      <h2>Figures overview</h2>
      <p class="muted" style="margin-bottom:.7rem">All generated figures. Click any image to zoom; use the download link to save. Figures are also embedded inline within their relevant report sections above.</p>
      <div class="figures-grid">{''.join(graphics_html) if graphics_html else '<p class="muted">No outbreak graphics found in exports/. Run the analysis pipeline to generate figures.</p>'}</div>
    </section>

    <!-- LIGHTBOX DIALOG -->
    <dialog class="lb" id="lightbox" aria-modal="true" aria-label="Figure zoom view">
      <div class="lb-inner">
        <img class="lb-img" id="lb-img" src="" alt="">
        <div class="lb-bar">
          <span class="lb-caption" id="lb-caption"></span>
          <a class="lb-dl" id="lb-dl" href="" download="">&#x2B07; Download</a>
          <button class="lb-close" onclick="document.getElementById('lightbox').close()" aria-label="Close zoom">&#x2715; Close</button>
        </div>
      </div>
    </dialog>

    <!-- KEY CONCEPTS -->
    <section class="card" id="concepts">
      <h2>Key concepts (Appendix D)</h2>
      <p class="muted" style="margin-bottom:.5rem">Click to expand each definition.</p>
      {concepts_html}
    </section>

    {raw_artifacts_html}

    <!-- FOOTER -->
    <section class="card" style="font-size:.82rem;color:var(--muted)">
      <p>Generated from TB Genomics backend artifacts &mdash; {_safe_html(generated_at)}. For operational use, interpret genomic findings with clinical history, contact tracing, epidemiology, QC status, and local governance review. This is a decision-support tool only.</p>
    </section>
  </main>
</div>

<script>
/* ── Active nav highlight ── */
(function(){{
  const links = document.querySelectorAll('nav.sidebar a');
  const sections = Array.from(links).map(a => document.querySelector(a.getAttribute('href'))).filter(Boolean);
  const obs = new IntersectionObserver(entries => {{
    entries.forEach(entry => {{
      if(entry.isIntersecting){{
        links.forEach(l => l.classList.remove('active'));
        const active = document.querySelector('nav.sidebar a[href="#'+entry.target.id+'"]');
        if(active) active.classList.add('active');
      }}
    }});
  }}, {{rootMargin: '-20% 0px -70% 0px'}});
  sections.forEach(s => obs.observe(s));
}})();

/* ── Lightbox ── */
const _figMeta = {{
  outbreaker_trace: {{title:'MCMC Log-Likelihood Trace', dl:'outbreaker_trace.png'}},
  outbreaker_hist:  {{title:'MCMC Log-Likelihood Distribution', dl:'outbreaker_hist.png'}},
  outbreaker_tree:  {{title:'Posterior Transmission Tree', dl:'outbreaker_tree.png'}},
  outbreaker_phylo: {{title:'Hierarchical Clustering Dendrogram', dl:'outbreaker_phylo.png'}},
  outbreaker_resistance: {{title:'Drug Resistance Profile Heatmap', dl:'outbreaker_resistance.png'}},
}};
function openLightbox(stem) {{
  const lb = document.getElementById('lightbox');
  const img = document.getElementById('lb-img');
  const cap = document.getElementById('lb-caption');
  const dlk = document.getElementById('lb-dl');
  const srcImg = document.querySelector('[data-stem="'+stem+'"] .fig-img');
  if(!srcImg) return;
  const meta = _figMeta[stem] || {{title: stem, dl: stem+'.png'}};
  img.src = srcImg.src;
  img.alt = meta.title;
  cap.textContent = meta.title;
  dlk.href = srcImg.src;
  dlk.download = meta.dl;
  lb.showModal();
}}
/* Close on backdrop click */
document.addEventListener('DOMContentLoaded', function(){{
  const lb = document.getElementById('lightbox');
  if(lb) lb.addEventListener('click', function(e){{
    if(e.target === lb) lb.close();
  }});
}});
</script>
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


# ── Case-specific comprehensive HTML report ──────────────────────────────────

@router.get("/case-report/{case_id}", response_class=HTMLResponse)
def case_report_html(case_id: str, db: Session = Depends(get_db)):  # noqa: C901
    """
    Generate a comprehensive, self-contained HTML report for a single case.

    Includes: identity, genomic profile, drug-resistance, QC metrics,
    cluster membership, transmission context, related-case timeline,
    and case-level audit entries.
    """
    import datetime as _dt

    # ── Resolve case ──────────────────────────────────────────────────────────
    pattern = f"{case_id}%" if len(case_id) < 36 else case_id
    core = db.execute(text("""
        SELECT
            c.pseudonymised_case_id::text  AS case_id,
            c.local_lab_sample_id,
            c.specimen_date,
            c.geographic_region,
            c.case_status,
            ti.lineage,
            ti.sublineage,
            ti.predicted_drug_resistance,
            ti.resistance_mutations,
            ti.interpretation_summary,
            cs.sequence,
            sqm.qc_status,
            sqm.coverage_breadth,
            sqm.mean_depth,
            sqm.contamination_flag,
            sqm.ambiguous_base_percent,
            cc.cluster_id::text            AS cluster_id,
            cl.snp_distance,
            cl.investigation_status        AS cluster_status,
            COUNT(cc2.sample_id) OVER (PARTITION BY cc.cluster_id) AS cluster_size
        FROM cases c
        LEFT JOIN tb_interpretation    ti  ON ti.sample_id  = c.pseudonymised_case_id
        LEFT JOIN consensus_sequences  cs  ON cs.sample_id  = c.pseudonymised_case_id
        LEFT JOIN sample_qc_metrics    sqm ON sqm.sample_id = c.pseudonymised_case_id
        LEFT JOIN case_clusters        cc  ON cc.sample_id  = c.pseudonymised_case_id
        LEFT JOIN clusters             cl  ON cl.cluster_id = cc.cluster_id
        LEFT JOIN case_clusters        cc2 ON cc2.cluster_id = cc.cluster_id
        WHERE CAST(c.pseudonymised_case_id AS TEXT) LIKE :pat
        LIMIT 1
    """), {"pat": pattern}).mappings().first()

    if not core:
        return HTMLResponse(
            content=f"<html><body><h2>Case not found: {html_lib.escape(case_id)}</h2></body></html>",
            status_code=404,
        )

    full_id   = core["case_id"]
    region    = core["geographic_region"] or "Unknown"
    short_id  = full_id[:8]
    generated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # ── Related cases in same cluster ────────────────────────────────────────
    cluster_peers: list[dict] = []
    if core["cluster_id"]:
        peer_rows = db.execute(text("""
            SELECT
                c.pseudonymised_case_id::text AS case_id,
                c.specimen_date,
                c.geographic_region,
                c.case_status,
                ti.lineage,
                ti.predicted_drug_resistance
            FROM case_clusters cc
            JOIN cases c ON c.pseudonymised_case_id = cc.sample_id
            LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
            WHERE cc.cluster_id = CAST(:cid AS uuid)
            ORDER BY c.specimen_date
        """), {"cid": core["cluster_id"]}).mappings().all()
        cluster_peers = [
            {
                "case_id":    str(r["case_id"])[:8],
                "date":       str(r["specimen_date"]),
                "region":     r["geographic_region"],
                "status":     r["case_status"],
                "lineage":    r["lineage"],
                "resistance": r["predicted_drug_resistance"],
                "is_index":   r["case_id"] == full_id,
            }
            for r in peer_rows
        ]

    # ── Regional case history timeline ───────────────────────────────────────
    history_rows = db.execute(text("""
        SELECT
            c.pseudonymised_case_id::text AS case_id,
            c.specimen_date,
            c.geographic_region,
            c.case_status,
            ti.lineage,
            ti.predicted_drug_resistance,
            cc.cluster_id::text AS cluster_id
        FROM cases c
        LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
        LEFT JOIN case_clusters cc     ON cc.sample_id = c.pseudonymised_case_id
        WHERE c.pseudonymised_case_id = CAST(:cid AS uuid)
           OR c.geographic_region = :region
        ORDER BY c.specimen_date
    """), {"cid": full_id, "region": region}).mappings().all()
    timeline = [
        {
            "case_id":    str(r["case_id"])[:8],
            "date":       str(r["specimen_date"]),
            "region":     r["geographic_region"],
            "status":     r["case_status"],
            "lineage":    r["lineage"],
            "resistance": r["predicted_drug_resistance"],
            "cluster_id": str(r["cluster_id"])[:8] if r["cluster_id"] else None,
            "is_index":   r["case_id"] == full_id,
        }
        for r in history_rows
    ]

    # ── Audit entries for this case ───────────────────────────────────────────
    audit_rows: list[dict] = []
    try:
        audit_rows = [
            dict(r) for r in db.execute(text("""
                SELECT action, user_id, timestamp, details
                FROM audit_log
                WHERE details ILIKE :pat
                ORDER BY timestamp DESC
                LIMIT 50
            """), {"pat": f"%{short_id}%"}).mappings().all()
        ]
    except Exception:
        audit_rows = []

    # ── Transmission network context ─────────────────────────────────────────
    tx_context: dict = {}
    tx_path = _export_path("transmission_network.json")
    if os.path.exists(tx_path):
        try:
            with open(tx_path, "r", encoding="utf-8") as _f:
                tx_data = json.load(_f)
            key_nodes = tx_data.get("key_nodes") or []
            edges     = tx_data.get("edges") or tx_data.get("transmission_edges") or []
            # find edges involving this case
            related_edges = [
                e for e in edges
                if short_id in str(e.get("from", "")) or short_id in str(e.get("to", ""))
                   or short_id in str(e.get("source", "")) or short_id in str(e.get("target", ""))
            ]
            node_match = next(
                (n for n in key_nodes if short_id in str(n.get("id", ""))), None
            )
            tx_context = {
                "is_key_node":    node_match is not None,
                "node_details":   node_match,
                "linked_edges":   related_edges[:20],
                "total_edges":    len(edges),
                "total_nodes":    len(key_nodes),
            }
        except Exception:
            tx_context = {}

    # ── TBProfiler JSON artifact ──────────────────────────────────────────────
    tbp_data: dict | None = None
    tbp_dir = _export_path("tbprofiler")
    if os.path.isdir(tbp_dir):
        for fname in os.listdir(tbp_dir):
            if short_id in fname and fname.endswith(".json"):
                try:
                    with open(os.path.join(tbp_dir, fname), "r", encoding="utf-8") as _f:
                        tbp_data = json.load(_f)
                except Exception:
                    pass
                break

    # ── Helper functions ──────────────────────────────────────────────────────
    def _e(v) -> str:
        return html_lib.escape("" if v is None else str(v), quote=True)

    def _badge_status(status: str | None) -> str:
        s = (status or "").lower()
        colour = {"open": "#e63946", "closed": "#2a9d8f", "active": "#e63946",
                  "pass": "#2a9d8f", "fail": "#e63946", "passed": "#2a9d8f",
                  "failed": "#e63946"}.get(s, "#6c757d")
        return (f'<span style="background:{colour};color:#fff;padding:2px 8px;'
                f'border-radius:10px;font-size:0.8em;font-weight:600">{_e(status or "Unknown")}</span>')

    def _resistance_badge(dr) -> str:
        if not dr:
            return '<span style="color:#6c757d;font-style:italic">Not determined</span>'
        dr_str = str(dr)
        if any(x in dr_str.lower() for x in ["xdr", "extensively"]):
            colour = "#7b2d8b"
        elif any(x in dr_str.lower() for x in ["mdr", "multi"]):
            colour = "#e63946"
        elif any(x in dr_str.lower() for x in ['"r"', "'r'", ": r", ":r", "resistant"]):
            colour = "#f4a261"
        else:
            colour = "#2a9d8f"
        return (f'<span style="background:{colour};color:#fff;padding:2px 8px;'
                f'border-radius:10px;font-size:0.8em;font-weight:600">{_e(dr_str[:80])}</span>')

    def _metric_card(label: str, value: str, sub: str = "", alert: bool = False) -> str:
        border = "#e63946" if alert else "#2a9d8f"
        return (
            f'<div style="background:#fff;border-left:4px solid {border};border-radius:6px;'
            f'padding:14px 18px;min-width:140px;box-shadow:0 1px 4px rgba(0,0,0,.08)">'
            f'<div style="font-size:.75em;color:#6c757d;text-transform:uppercase;letter-spacing:.04em">{_e(label)}</div>'
            f'<div style="font-size:1.6em;font-weight:700;color:#212529;line-height:1.2">{value}</div>'
            f'{"<div style=font-size:.8em;color:#6c757d;margin-top:2px>" + _e(sub) + "</div>" if sub else ""}'
            f'</div>'
        )

    def _section(title: str, body: str) -> str:
        return (
            f'<section style="margin-bottom:32px">'
            f'<h2 style="font-size:1.1em;font-weight:700;color:#1d3557;border-bottom:2px solid #e9ecef;'
            f'padding-bottom:6px;margin-bottom:14px">{_e(title)}</h2>'
            f'{body}</section>'
        )

    def _kv_table(rows: list[tuple[str, str]]) -> str:
        tr = "".join(
            f'<tr><th style="width:220px;text-align:left;padding:6px 10px;color:#495057;'
            f'font-weight:600;background:#f8f9fa">{_e(k)}</th>'
            f'<td style="padding:6px 10px">{v}</td></tr>'
            for k, v in rows
        )
        return (
            '<table style="width:100%;border-collapse:collapse;border:1px solid #dee2e6;'
            'border-radius:4px;overflow:hidden"><tbody>' + tr + '</tbody></table>'
        )

    def _data_table(headers: list[str], rows: list[list[str]], empty: str = "No data") -> str:
        if not rows:
            return f'<p style="color:#6c757d;font-style:italic">{_e(empty)}</p>'
        th = "".join(
            f'<th style="padding:7px 10px;background:#1d3557;color:#fff;text-align:left;'
            f'font-weight:600;font-size:.85em">{_e(h)}</th>'
            for h in headers
        )
        tr_html = ""
        for i, row in enumerate(rows):
            bg = "#f8f9fa" if i % 2 else "#fff"
            tr_html += (
                '<tr style="background:' + bg + '">'
                + "".join(f'<td style="padding:6px 10px;font-size:.875em;border-top:1px solid #dee2e6">{c}</td>' for c in row)
                + "</tr>"
            )
        return (
            '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">'
            '<thead><tr>' + th + '</tr></thead><tbody>' + tr_html + '</tbody></table></div>'
        )

    # ── Build report sections ─────────────────────────────────────────────────

    # 1. Identity card
    identity_body = _kv_table([
        ("Pseudonymised Case ID",    _e(full_id)),
        ("Short Reference",          _e(short_id)),
        ("Local Lab Sample ID",      _e(core["local_lab_sample_id"] or "—")),
        ("Specimen Date",            _e(core["specimen_date"])),
        ("Geographic Region",        _e(region)),
        ("Case Status",              _badge_status(core["case_status"])),
        ("Report Generated",         _e(generated)),
    ])
    identity_sec = _section("1. Case Identity", identity_body)

    # 2. Genomic / Lineage profile
    lineage_body = _kv_table([
        ("Lineage",          _e(core["lineage"] or "Not determined")),
        ("Sublineage",       _e(core["sublineage"] or "—")),
        ("Sequence Present", _e("Yes" if core["sequence"] else "No")),
        ("Interpretation",   _e(core["interpretation_summary"] or "—")),
    ])
    lineage_sec = _section("2. Genomic &amp; Lineage Profile", lineage_body)

    # 3. Drug resistance
    dr_raw = core["predicted_drug_resistance"]
    if isinstance(dr_raw, dict):
        dr_rows: list[list[str]] = [
            [_e(drug), _badge_status(result)]
            for drug, result in dr_raw.items()
        ]
        dr_table = _data_table(["Drug", "Predicted Result"], dr_rows, "No resistance data")
    else:
        dr_table = f'<p>{_resistance_badge(dr_raw)}</p>'

    mut_raw = core["resistance_mutations"]
    if isinstance(mut_raw, list) and mut_raw:
        mut_table = _data_table(
            ["Gene / Mutation", "Drug", "Confidence"],
            [[_e(str(m.get("mutation", m) if isinstance(m, dict) else m)),
              _e(str(m.get("drug", "—") if isinstance(m, dict) else "—")),
              _e(str(m.get("confidence", "—") if isinstance(m, dict) else "—"))]
             for m in mut_raw[:30]],
            "No mutation data"
        )
    elif mut_raw:
        mut_table = f'<pre style="font-size:.8em">{_e(str(mut_raw)[:1000])}</pre>'
    else:
        mut_table = '<p style="color:#6c757d;font-style:italic">No resistance mutations recorded</p>'

    dr_sec = _section(
        "3. Drug Resistance Profile",
        '<h3 style="font-size:.95em;margin:0 0 8px;color:#495057">Predicted Resistance</h3>'
        + dr_table
        + '<h3 style="font-size:.95em;margin:16px 0 8px;color:#495057">Resistance Mutations</h3>'
        + mut_table,
    )

    # 4. QC metrics
    qc_alert = (
        str(core.get("qc_status", "")).lower() in {"fail", "failed"}
        or bool(core.get("contamination_flag"))
    )
    qc_body = _kv_table([
        ("QC Status",             _badge_status(core["qc_status"])),
        ("Coverage Breadth",      _e(f"{core['coverage_breadth']:.1f}%" if core["coverage_breadth"] is not None else "—")),
        ("Mean Depth",            _e(f"{core['mean_depth']:.1f}×" if core["mean_depth"] is not None else "—")),
        ("Contamination Flag",    _e("⚠ YES" if core["contamination_flag"] else "No")),
        ("Ambiguous Bases (%)",   _e(f"{core['ambiguous_base_percent']:.2f}%" if core["ambiguous_base_percent"] is not None else "—")),
    ])
    qc_sec = _section(
        "4. Sequencing QC Metrics" + (" ⚠" if qc_alert else ""),
        qc_body,
    )

    # 5. Cluster membership
    if core["cluster_id"]:
        cluster_body = _kv_table([
            ("Cluster ID",           _e(core["cluster_id"][:8])),
            ("Cluster Size",         _e(str(core["cluster_size"]))),
            ("SNP Distance (max)",   _e(str(core["snp_distance"]) if core["snp_distance"] is not None else "—")),
            ("Investigation Status", _badge_status(core["cluster_status"])),
        ])
        peer_table = _data_table(
            ["Case ID", "Date", "Region", "Lineage", "Resistance", "Status"],
            [
                [
                    '<strong>' + _e(p["case_id"]) + '</strong>' if p["is_index"] else _e(p["case_id"]),
                    _e(p["date"]),
                    _e(p["region"]),
                    _e(p["lineage"] or "—"),
                    _resistance_badge(p["resistance"]),
                    _badge_status(p["status"]),
                ]
                for p in cluster_peers
            ],
            "No cluster peers found",
        )
        cluster_sec = _section(
            "5. Cluster Membership",
            cluster_body
            + '<h3 style="font-size:.95em;margin:16px 0 8px;color:#495057">Cluster Members</h3>'
            + peer_table,
        )
    else:
        cluster_sec = _section(
            "5. Cluster Membership",
            '<p style="color:#6c757d;font-style:italic">This case is not assigned to any cluster.</p>',
        )

    # 6. Transmission network context
    if tx_context:
        tx_rows = _kv_table([
            ("Is Key Network Node",      _e("Yes" if tx_context.get("is_key_node") else "No")),
            ("Linked Transmission Edges", _e(str(len(tx_context.get("linked_edges", []))))),
            ("Total Network Edges",       _e(str(tx_context.get("total_edges", "—")))),
            ("Total Key Nodes",           _e(str(tx_context.get("total_nodes", "—")))),
        ])
        edge_data = tx_context.get("linked_edges", [])
        if edge_data:
            edge_table = _data_table(
                ["From", "To", "Probability / Weight"],
                [
                    [
                        _e(str(e.get("from", e.get("source", "—")))[:10]),
                        _e(str(e.get("to",   e.get("target", "—")))[:10]),
                        _e(str(e.get("probability", e.get("weight", "—")))),
                    ]
                    for e in edge_data
                ],
                "No linked edges",
            )
        else:
            edge_table = '<p style="color:#6c757d;font-style:italic">No direct transmission edges found for this case.</p>'
        tx_sec = _section(
            "6. Transmission Network Context",
            tx_rows
            + '<h3 style="font-size:.95em;margin:16px 0 8px;color:#495057">Linked Edges</h3>'
            + edge_table,
        )
    else:
        tx_sec = _section(
            "6. Transmission Network Context",
            '<p style="color:#6c757d;font-style:italic">No transmission network data available. Run the outbreaker2 analysis first.</p>',
        )

    # 7. Regional case timeline
    timeline_sec = _section(
        "7. Regional Case Timeline",
        _data_table(
            ["Case ID", "Date", "Region", "Lineage", "Resistance", "Cluster", "Status"],
            [
                [
                    '<strong>' + _e(t["case_id"]) + '</strong>' if t["is_index"] else _e(t["case_id"]),
                    _e(t["date"]),
                    _e(t["region"]),
                    _e(t["lineage"] or "—"),
                    _resistance_badge(t["resistance"]),
                    _e(t["cluster_id"] or "—"),
                    _badge_status(t["status"]),
                ]
                for t in timeline
            ],
            "No timeline data",
        ),
    )

    # 8. TBProfiler data
    if tbp_data and isinstance(tbp_data, dict):
        tbp_fields = [
            ("TBProfiler Version",    _e(tbp_data.get("tbprofiler_version", "—"))),
            ("Main Lineage",          _e(tbp_data.get("main_lin", "—"))),
            ("Sub Lineage",           _e(tbp_data.get("sub_lin", "—"))),
            ("DR Type",               _e(tbp_data.get("drtype", "—"))),
            ("Median Coverage",       _e(str(tbp_data.get("median_coverage", "—")))),
            ("Pct Reads Mapped",      _e(str(tbp_data.get("pct_reads_mapped", "—")))),
        ]
        tbp_sec = _section("8. TBProfiler Analysis Details", _kv_table(tbp_fields))
    else:
        tbp_sec = _section(
            "8. TBProfiler Analysis Details",
            '<p style="color:#6c757d;font-style:italic">No TBProfiler output found for this case.</p>',
        )

    # 9. Audit trail
    if audit_rows:
        audit_sec = _section(
            "9. Case Audit Trail",
            _data_table(
                ["Timestamp", "Action", "User", "Details"],
                [
                    [
                        _e(str(a.get("timestamp", "—"))[:19]),
                        _e(str(a.get("action", "—"))),
                        _e(str(a.get("user_id", "—"))),
                        _e(str(a.get("details", ""))[:120]),
                    ]
                    for a in audit_rows
                ],
                "No audit entries",
            ),
        )
    else:
        audit_sec = _section(
            "9. Case Audit Trail",
            '<p style="color:#6c757d;font-style:italic">No audit entries found referencing this case.</p>',
        )

    # ── Assemble HTML ─────────────────────────────────────────────────────────
    dr_summary_text = _e(str(core["predicted_drug_resistance"])[:60]) if core["predicted_drug_resistance"] else "Not determined"
    lineage_text    = _e(core["lineage"] or "Unknown")
    status_badge    = _badge_status(core["case_status"])

    metric_strip = (
        '<div style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:28px">'
        + _metric_card("Case ID",     short_id)
        + _metric_card("Region",      region)
        + _metric_card("Lineage",     core["lineage"] or "—")
        + _metric_card("Cluster",     core["cluster_id"][:8] if core["cluster_id"] else "None",
                        sub=f"{core['cluster_size']} members" if core["cluster_id"] else "")
        + _metric_card("QC Status",   core["qc_status"] or "—",
                        alert=qc_alert)
        + _metric_card("Timeline Cases", str(len(timeline)))
        + '</div>'
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Case Report – {_e(short_id)}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f0f4f8; color: #212529; font-size: 14px; line-height: 1.5;
  }}
  .report-header {{
    background: linear-gradient(135deg, #1d3557 0%, #457b9d 100%);
    color: #fff; padding: 28px 40px 20px;
  }}
  .report-header h1 {{ margin: 0 0 4px; font-size: 1.6em; }}
  .report-header p  {{ margin: 0; opacity: .8; font-size: .9em; }}
  .report-body {{
    max-width: 1100px; margin: 28px auto; padding: 0 24px;
  }}
  .data-card {{
    background: #fff; border-radius: 8px; box-shadow: 0 1px 6px rgba(0,0,0,.09);
    padding: 28px 32px; margin-bottom: 24px;
  }}
  @media print {{
    body {{ background: #fff; }}
    .report-header {{ background: #1d3557 !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
    .data-card {{ box-shadow: none; border: 1px solid #dee2e6; }}
    .no-print {{ display: none; }}
  }}
</style>
</head>
<body>
<div class="report-header">
  <h1>TB Case Investigation Report</h1>
  <p>Case {_e(short_id)} &nbsp;·&nbsp; {_e(region)} &nbsp;·&nbsp;
     Status: {status_badge} &nbsp;·&nbsp; Generated {_e(generated)}</p>
</div>
<div class="report-body">
  <div class="no-print" style="margin-bottom:18px">
    <button onclick="window.print()"
      style="padding:8px 20px;background:#1d3557;color:#fff;border:none;border-radius:4px;
             cursor:pointer;font-size:.9em;margin-right:8px">
      &#x1F5B6; Print / Save as PDF
    </button>
    <button onclick="window.close()"
      style="padding:8px 20px;background:#6c757d;color:#fff;border:none;border-radius:4px;
             cursor:pointer;font-size:.9em">
      Close
    </button>
  </div>
  {metric_strip}
  <div class="data-card">
    {identity_sec}
    {lineage_sec}
    {dr_sec}
    {qc_sec}
    {cluster_sec}
    {tx_sec}
    {timeline_sec}
    {tbp_sec}
    {audit_sec}
  </div>
  <p style="text-align:center;color:#adb5bd;font-size:.8em;margin-top:24px">
    TB Genomic Surveillance Platform &nbsp;·&nbsp; Confidential &nbsp;·&nbsp;
    For authorised public-health use only
  </p>
</div>
</body>
</html>"""

    return HTMLResponse(content=html)
