import json
import os

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.data_safety import get_data_safety_status
from backend.models import Case, TbInterpretation
from backend.routers.dependencies import get_db
from backend.runtime_paths import export_path


router = APIRouter(prefix="/cases", tags=["cases"])

def _export_path(*parts: str) -> str:
    """Return an absolute path under the repository export directory."""
    return export_path(*parts)


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

    lineage_rows = db.execute(
        text(
            """
            SELECT COALESCE(NULLIF(TRIM(ti.lineage), ''), 'unknown') AS lineage,
                   COUNT(DISTINCT c.pseudonymised_case_id)::int AS case_count
            FROM cases c
            LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
            WHERE c.specimen_date >= CURRENT_DATE - (:weeks * INTERVAL '7 days')
            GROUP BY COALESCE(NULLIF(TRIM(ti.lineage), ''), 'unknown')
            ORDER BY case_count DESC, lineage ASC
            """
        ),
        params,
    ).mappings().all()

    growth_row = db.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE specimen_date >= CURRENT_DATE - INTERVAL '30 days')::int AS cases_last_30,
                COUNT(*) FILTER (
                    WHERE specimen_date >= CURRENT_DATE - INTERVAL '60 days'
                      AND specimen_date < CURRENT_DATE - INTERVAL '30 days'
                )::int AS cases_prev_30,
                COUNT(*) FILTER (WHERE specimen_date >= CURRENT_DATE - INTERVAL '60 days')::int AS cases_last_60,
                COUNT(*) FILTER (
                    WHERE specimen_date >= CURRENT_DATE - INTERVAL '120 days'
                      AND specimen_date < CURRENT_DATE - INTERVAL '60 days'
                )::int AS cases_prev_60,
                COUNT(*) FILTER (WHERE specimen_date >= CURRENT_DATE - INTERVAL '90 days')::int AS cases_last_90,
                COUNT(*) FILTER (
                    WHERE specimen_date >= CURRENT_DATE - INTERVAL '180 days'
                      AND specimen_date < CURRENT_DATE - INTERVAL '90 days'
                )::int AS cases_prev_90
            FROM cases
            """
        )
    ).mappings().first() or {}

    sequence_summary = {}
    sequence_summary_path = _export_path("sequence_clustering_summary.json")
    if os.path.exists(sequence_summary_path):
        try:
            with open(sequence_summary_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
                if isinstance(payload, dict):
                    sequence_summary = payload
        except Exception:
            sequence_summary = {}

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
        "lineage_distribution": [
            {
                "lineage": str(row.get("lineage") or "unknown"),
                "case_count": _to_int(row.get("case_count")),
            }
            for row in lineage_rows
        ],
        "cluster_growth": {
            "last_30_days": _to_int(growth_row.get("cases_last_30")),
            "previous_30_days": _to_int(growth_row.get("cases_prev_30")),
            "last_60_days": _to_int(growth_row.get("cases_last_60")),
            "previous_60_days": _to_int(growth_row.get("cases_prev_60")),
            "last_90_days": _to_int(growth_row.get("cases_last_90")),
            "previous_90_days": _to_int(growth_row.get("cases_prev_90")),
        },
        "sequence_clustering_quality": {
            "pairwise_comparable_sites": sequence_summary.get("pairwise_comparable_sites"),
            "link_pair_comparable_sites": sequence_summary.get("link_pair_comparable_sites"),
            "pairwise_snp_distance_histogram": sequence_summary.get("pairwise_snp_distance_histogram"),
        },
        "representativeness_by_region": [
            {
                "region": row["region"],
                "eligible_cases": _to_int(row["eligible_cases"]),
                "sequenced_cases": _to_int(row["sequenced_cases"]),
                "sequenced_pct": _to_optional_pct(
                    _to_int(row["sequenced_cases"]), _to_int(row["eligible_cases"])
                ),
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
    """List cases with lineage and interpretation data joined."""
    rows = db.execute(
        text("""
            SELECT 
                c.pseudonymised_case_id,
                c.local_lab_sample_id,
                c.specimen_date,
                c.geographic_region,
                c.case_status,
                c.created_at,
                c.symptom_onset_date,
                c.treatment_start_date,
                c.smear_status,
                c.cavitation_status,
                c.culture_status,
                c.culture_positivity_duration_days,
                c.infectiousness_notes,
                COALESCE(ti.lineage, 'Unknown') AS lineage,
                COALESCE(ti.sublineage, 'Unknown') AS sublineage,
                ti.species_confirmation,
                ti.predicted_drug_resistance
            FROM cases c
            LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
            ORDER BY c.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        {"limit": limit, "offset": offset}
    ).mappings().all()
    
    return [dict(row) for row in rows]


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
    clustered = db.execute(text("SELECT COUNT(DISTINCT sample_id) FROM case_clusters")).scalar() or 0
    open_clusters = (
        db.execute(text("SELECT COUNT(*) FROM clusters WHERE investigation_status = 'open'")).scalar()
        or 0
    )
    return {
        "total_cases": total,
        "clustered_cases": clustered,
        "unclustered_cases": max(0, total - clustered),
        "open_clusters": open_clusters,
    }


@router.get("/data-readiness")
def data_readiness(db: Session = Depends(get_db)):
    """Return completeness metrics needed before analysis and reporting."""
    has_sequences = _table_exists(db, "consensus_sequences")
    has_qc = _table_exists(db, "sample_qc_metrics")
    has_interpretation = _table_exists(db, "tb_interpretation")

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
    interpretation_join = (
        "LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id"
        if has_interpretation
        else ""
    )

    sequenced_expr = "COUNT(DISTINCT cs.sample_id)::int" if has_sequences else "0::int"
    qc_expr = "COUNT(DISTINCT sqm.sample_id)::int" if has_qc else "0::int"
    lineage_expr = (
        "COUNT(DISTINCT c.pseudonymised_case_id) FILTER (WHERE NULLIF(TRIM(ti.lineage), '') IS NOT NULL)::int"
        if has_interpretation
        else "0::int"
    )
    resistance_expr = (
        """
        COUNT(DISTINCT c.pseudonymised_case_id) FILTER (
            WHERE ti.predicted_drug_resistance IS NOT NULL
              AND ti.predicted_drug_resistance::text NOT IN ('null', '{}', '[]')
        )::int
        """
        if has_interpretation
        else "0::int"
    )

    row = db.execute(
        text(
            f"""
            SELECT
                COUNT(DISTINCT c.pseudonymised_case_id)::int AS total_cases,
                {sequenced_expr} AS sequenced_cases,
                {qc_expr} AS qc_complete_cases,
                COUNT(DISTINCT c.pseudonymised_case_id) FILTER (
                    WHERE NULLIF(TRIM(COALESCE(c.geographic_region, '')), '') IS NULL
                )::int AS missing_geography,
                COUNT(DISTINCT c.pseudonymised_case_id) FILTER (
                    WHERE c.specimen_date IS NULL
                )::int AS missing_dates,
                {lineage_expr} AS lineage_called_cases,
                {resistance_expr} AS resistance_called_cases
            FROM cases c
            {sequence_join}
            {qc_join}
            {interpretation_join}
            """
        )
    ).mappings().first() or {}

    total_cases = _to_int(row.get("total_cases"))
    sequenced_cases = _to_int(row.get("sequenced_cases"))
    qc_complete_cases = _to_int(row.get("qc_complete_cases"))
    lineage_called_cases = _to_int(row.get("lineage_called_cases"))
    resistance_called_cases = _to_int(row.get("resistance_called_cases"))
    missing_geography = _to_int(row.get("missing_geography"))
    missing_dates = _to_int(row.get("missing_dates"))

    checks = [
        {
            "key": "sequencing_coverage",
            "label": "Sequencing coverage",
            "complete": sequenced_cases,
            "missing": max(0, total_cases - sequenced_cases),
            "percent": _to_optional_pct(sequenced_cases, total_cases),
        },
        {
            "key": "qc_completeness",
            "label": "QC completeness",
            "complete": qc_complete_cases,
            "missing": max(0, total_cases - qc_complete_cases),
            "percent": _to_optional_pct(qc_complete_cases, total_cases),
        },
        {
            "key": "geography",
            "label": "Geography present",
            "complete": max(0, total_cases - missing_geography),
            "missing": missing_geography,
            "percent": _to_optional_pct(max(0, total_cases - missing_geography), total_cases),
        },
        {
            "key": "specimen_dates",
            "label": "Specimen dates present",
            "complete": max(0, total_cases - missing_dates),
            "missing": missing_dates,
            "percent": _to_optional_pct(max(0, total_cases - missing_dates), total_cases),
        },
        {
            "key": "lineage_calls",
            "label": "Lineage calls",
            "complete": lineage_called_cases,
            "missing": max(0, total_cases - lineage_called_cases),
            "percent": _to_optional_pct(lineage_called_cases, total_cases),
        },
        {
            "key": "resistance_calls",
            "label": "Resistance calls",
            "complete": resistance_called_cases,
            "missing": max(0, total_cases - resistance_called_cases),
            "percent": _to_optional_pct(resistance_called_cases, total_cases),
        },
    ]

    blockers = [check for check in checks if check["missing"] > 0]
    return {
        "total_cases": total_cases,
        "sequencing_coverage": {
            "sequenced_cases": sequenced_cases,
            "percent": _to_optional_pct(sequenced_cases, total_cases),
        },
        "qc_completeness": {
            "qc_complete_cases": qc_complete_cases,
            "percent": _to_optional_pct(qc_complete_cases, total_cases),
        },
        "missing_geography": missing_geography,
        "missing_dates": missing_dates,
        "missing_lineage_calls": max(0, total_cases - lineage_called_cases),
        "missing_resistance_calls": max(0, total_cases - resistance_called_cases),
        "checks": checks,
        "status": "ready" if total_cases > 0 and not blockers else "needs_review",
    }


@router.get("/data-safety")
def data_safety(db: Session = Depends(get_db)):
    """Return whether the current dataset is operational or synthetic/demo."""
    return get_data_safety_status(db)


@router.get("/outbreaker-status")
def outbreaker_status():
    summary_path = _export_path("outbreaker_summary.json")
    provenance = None
    if os.path.exists(summary_path):
        try:
            with open(summary_path, encoding="utf-8") as f:
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
