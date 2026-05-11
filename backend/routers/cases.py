
import os
import json

from datetime import datetime
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from backend.database import SessionLocal
from backend.models import Case
from backend.data_safety import enforce_operational_dataset, get_data_safety_status

router = APIRouter(prefix="/cases", tags=["cases"])

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()


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

    # TB-Profiler effective status
    tb_effective = tb_local
    if tb_run.get("status") == "completed":
        tb_effective = f"available_via_{tb_run.get('runner', 'runner')}"
    elif tb_wsl_run.get("status") == "completed" or wsl_tb == "installed":
        tb_effective = "available_via_wsl"
    elif tb_docker_run.get("status") == "completed":
        tb_effective = "available_via_docker"

    # Mykrobe effective status
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

@router.get("/")
def list_cases(db: Session = Depends(get_db)):
    return db.query(Case).all()


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    total_cases = db.execute(text("SELECT COUNT(*) FROM cases")).scalar() or 0
    clustered_cases = db.execute(text("SELECT COUNT(DISTINCT sample_id) FROM case_clusters")).scalar() or 0
    open_clusters = db.execute(
        text("SELECT COUNT(*) FROM clusters WHERE investigation_status = 'open'")
    ).scalar() or 0

    return {
        "total_cases": int(total_cases),
        "clustered_cases": int(clustered_cases),
        "unclustered_cases": int(total_cases) - int(clustered_cases),
        "open_clusters": int(open_clusters),
    }


@router.get("/data-safety")
def data_safety(db: Session = Depends(get_db)):
    return get_data_safety_status(db)


@router.get("/surveillance-kpis")
def surveillance_kpis(weeks: int = 12, db: Session = Depends(get_db)):
    """Programme-level surveillance metrics aligned with WGS reporting practice."""
    weeks = max(1, min(52, int(weeks)))

    qc_table_exists = db.execute(
        text("SELECT to_regclass('public.sample_qc_metrics') IS NOT NULL")
    ).scalar()

    if not qc_table_exists:
        base = db.execute(
            text(
                """
                WITH window_cases AS (
                    SELECT *
                    FROM cases
                    WHERE specimen_date >= CURRENT_DATE - (CAST(:weeks AS int) * INTERVAL '7 days')
                ),
                totals AS (
                    SELECT
                        COUNT(*)::int AS eligible_cases,
                        COUNT(*) FILTER (WHERE cs.sample_id IS NOT NULL)::int AS sequenced_cases
                    FROM window_cases wc
                    LEFT JOIN consensus_sequences cs ON cs.sample_id = wc.pseudonymised_case_id
                ),
                region_counts AS (
                    SELECT
                        geographic_region,
                        COUNT(*)::int AS eligible_cases,
                        COUNT(*) FILTER (WHERE cs.sample_id IS NOT NULL)::int AS sequenced_cases
                    FROM window_cases wc
                    LEFT JOIN consensus_sequences cs ON cs.sample_id = wc.pseudonymised_case_id
                    GROUP BY geographic_region
                )
                SELECT
                    totals.eligible_cases,
                    totals.sequenced_cases,
                    COALESCE(
                        (
                            SELECT JSON_AGG(
                                JSON_BUILD_OBJECT(
                                    'region', geographic_region,
                                    'eligible_cases', eligible_cases,
                                    'sequenced_cases', sequenced_cases,
                                    'sequenced_pct', CASE
                                        WHEN eligible_cases = 0 THEN NULL
                                        ELSE ROUND((sequenced_cases::numeric / eligible_cases::numeric) * 100.0, 2)
                                    END
                                )
                                ORDER BY geographic_region
                            )
                            FROM region_counts
                        ),
                        '[]'::json
                    ) AS representativeness_by_region
                FROM totals
                """
            ),
            {"weeks": weeks},
        ).mappings().first()

        eligible_cases = int((base or {}).get("eligible_cases") or 0)
        sequenced_cases = int((base or {}).get("sequenced_cases") or 0)
        sequenced_pct = round((sequenced_cases / eligible_cases) * 100.0, 2) if eligible_cases else None

        return {
            "window_weeks": weeks,
            "eligible_cases": eligible_cases,
            "sequenced_cases": sequenced_cases,
            "sequenced_pct": sequenced_pct,
            "qc_reported_cases": 0,
            "qc_pass_cases": 0,
            "qc_fail_cases": 0,
            "qc_pass_pct": None,
            "contamination_flag_cases": 0,
            "median_days_specimen_to_qc": None,
            "representativeness_by_region": (base or {}).get("representativeness_by_region") or [],
            "warning": "sample_qc_metrics table not found; apply updated db/schema.sql to enable QC KPIs.",
        }

    kpi_rows = db.execute(
        text(
            """
            WITH window_cases AS (
                SELECT *
                FROM cases
                WHERE specimen_date >= CURRENT_DATE - (CAST(:weeks AS int) * INTERVAL '7 days')
            ),
            totals AS (
                SELECT
                    COUNT(*)::int AS eligible_cases,
                    COUNT(*) FILTER (WHERE cs.sample_id IS NOT NULL)::int AS sequenced_cases,
                    COUNT(*) FILTER (WHERE sqm.sample_id IS NOT NULL)::int AS qc_reported_cases,
                    COUNT(*) FILTER (
                        WHERE LOWER(COALESCE(sqm.qc_status, '')) IN ('pass', 'passed')
                    )::int AS qc_pass_cases,
                    COUNT(*) FILTER (
                        WHERE LOWER(COALESCE(sqm.qc_status, '')) IN ('fail', 'failed')
                    )::int AS qc_fail_cases,
                    COUNT(*) FILTER (WHERE COALESCE(sqm.contamination_flag, FALSE))::int AS contamination_flag_cases
                FROM window_cases wc
                LEFT JOIN consensus_sequences cs ON cs.sample_id = wc.pseudonymised_case_id
                LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id = wc.pseudonymised_case_id
            ),
            region_counts AS (
                SELECT
                    geographic_region,
                    COUNT(*)::int AS eligible_cases,
                    COUNT(*) FILTER (WHERE cs.sample_id IS NOT NULL)::int AS sequenced_cases
                FROM window_cases wc
                LEFT JOIN consensus_sequences cs ON cs.sample_id = wc.pseudonymised_case_id
                GROUP BY geographic_region
            ),
            lag_stats AS (
                SELECT
                    PERCENTILE_CONT(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (sqm.reported_at::timestamp - wc.specimen_date::timestamp)) / 86400.0
                    ) AS median_days_specimen_to_qc
                FROM window_cases wc
                JOIN sample_qc_metrics sqm ON sqm.sample_id = wc.pseudonymised_case_id
                WHERE sqm.reported_at IS NOT NULL
            )
            SELECT
                totals.eligible_cases,
                totals.sequenced_cases,
                totals.qc_reported_cases,
                totals.qc_pass_cases,
                totals.qc_fail_cases,
                totals.contamination_flag_cases,
                lag_stats.median_days_specimen_to_qc,
                COALESCE(
                    (
                        SELECT JSON_AGG(
                            JSON_BUILD_OBJECT(
                                'region', geographic_region,
                                'eligible_cases', eligible_cases,
                                'sequenced_cases', sequenced_cases,
                                'sequenced_pct', CASE
                                    WHEN eligible_cases = 0 THEN NULL
                                    ELSE ROUND((sequenced_cases::numeric / eligible_cases::numeric) * 100.0, 2)
                                END
                            )
                            ORDER BY geographic_region
                        )
                        FROM region_counts
                    ),
                    '[]'::json
                ) AS representativeness_by_region
            FROM totals
            CROSS JOIN lag_stats
            """
        ),
        {"weeks": weeks},
    ).mappings().first()

    if not kpi_rows:
        return {
            "window_weeks": weeks,
            "eligible_cases": 0,
            "sequenced_cases": 0,
            "sequenced_pct": None,
            "qc_reported_cases": 0,
            "qc_pass_cases": 0,
            "qc_fail_cases": 0,
            "qc_pass_pct": None,
            "contamination_flag_cases": 0,
            "median_days_specimen_to_qc": None,
            "representativeness_by_region": [],
        }

    eligible_cases = int(kpi_rows["eligible_cases"] or 0)
    sequenced_cases = int(kpi_rows["sequenced_cases"] or 0)
    qc_reported_cases = int(kpi_rows["qc_reported_cases"] or 0)
    qc_pass_cases = int(kpi_rows["qc_pass_cases"] or 0)

    sequenced_pct = None
    if eligible_cases:
        sequenced_pct = round((sequenced_cases / eligible_cases) * 100.0, 2)

    qc_pass_pct = None
    if qc_reported_cases:
        qc_pass_pct = round((qc_pass_cases / qc_reported_cases) * 100.0, 2)

    median_days = kpi_rows["median_days_specimen_to_qc"]
    if median_days is not None:
        median_days = round(float(median_days), 2)

    return {
        "window_weeks": weeks,
        "eligible_cases": eligible_cases,
        "sequenced_cases": sequenced_cases,
        "sequenced_pct": sequenced_pct,
        "qc_reported_cases": qc_reported_cases,
        "qc_pass_cases": qc_pass_cases,
        "qc_fail_cases": int(kpi_rows["qc_fail_cases"] or 0),
        "qc_pass_pct": qc_pass_pct,
        "contamination_flag_cases": int(kpi_rows["contamination_flag_cases"] or 0),
        "median_days_specimen_to_qc": median_days,
        "representativeness_by_region": kpi_rows["representativeness_by_region"] or [],
    }


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
    summary_path = "exports/outbreaker_summary.json"
    provenance = None
    if os.path.exists(summary_path):
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                summary = json.load(f)
                provenance = (summary or {}).get("data_provenance")
        except Exception:
            provenance = None

    return {
        "cases_export": os.path.exists("exports/cases.csv"),
        "dna_export": os.path.exists("exports/dna.fasta"),
        "results_rds": os.path.exists("outbreaker2_results.rds"),
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
    summary_path = "exports/outbreaker_summary.json"
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

    network_path = "exports/transmission_network.json"
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

    secondary_path = "exports/secondary_engine_validation.json"
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

    path = "exports/lineage_dr_validation.json"
    if not os.path.exists(path):
        return {
            "status": "no_results",
            "message": "Lineage/DR validation has not been run yet",
            "artifact_path": path,
            "analysis_summary": analysis_summary,
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
        }

    payload["artifact_path"] = path
    payload["analysis_summary"] = analysis_summary
    payload["effective_engines"] = _derive_effective_engine_status(payload)
    return payload


@router.get("/outbreak-report")
def outbreak_report(db: Session = Depends(get_db)):
    """Generate and return a PDF outbreak investigation report."""
    enforce_operational_dataset(db, "cases/outbreak-report")

    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        from reportlab.lib.utils import ImageReader
    except Exception as e:
        return {"error": f"PDF generation dependency missing: {e}"}

    os.makedirs("exports", exist_ok=True)
    report_path = os.path.join("exports", "outbreaker_investigation_report.pdf")

    total_cases = db.execute(text("SELECT COUNT(*) FROM cases")).scalar() or 0
    clustered_cases = db.execute(text("SELECT COUNT(DISTINCT sample_id) FROM case_clusters")).scalar() or 0
    open_clusters = db.execute(
        text("SELECT COUNT(*) FROM clusters WHERE investigation_status = 'open'")
    ).scalar() or 0

    summary_data = None
    summary_path = os.path.join("exports", "outbreaker_summary.json")
    if os.path.exists(summary_path):
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                summary_data = json.load(f)
        except Exception:
            summary_data = None

    transmission_data = None
    transmission_path = os.path.join("exports", "transmission_network.json")
    if os.path.exists(transmission_path):
        try:
            with open(transmission_path, "r", encoding="utf-8") as f:
                transmission_data = json.load(f)
        except Exception:
            transmission_data = None

    def load_json_artifact(filename: str):
        path = os.path.join("exports", filename)
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

    graphic_files = [
        "outbreaker_trace.png",
        "outbreaker_hist.png",
        "outbreaker_tree.png",
        "outbreaker_phylo.png",
        "outbreaker_resistance.png",
    ]
    existing_graphics = [g for g in graphic_files if os.path.exists(os.path.join("exports", g))]

    def build_report_image(image_path: str):
        max_width = 6.4 * inch
        max_height = 5.2 * inch
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

            chart_path = os.path.join("exports", "outbreaker_weekly_trends.png")
            plt.savefig(chart_path, dpi=140)
            plt.close()
            return chart_path
        except Exception:
            return None

    doc = SimpleDocTemplate(report_path, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()

    # ── Custom styles ──────────────────────────────────────────────────────────
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY

    caption_style = ParagraphStyle(
        "Caption",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#4a6a7a"),
        leading=11,
        spaceAfter=4,
        italic=True,
    )
    callout_style = ParagraphStyle(
        "Callout",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#184e44"),
        backColor=colors.HexColor("#eef8f6"),
        borderColor=colors.HexColor("#7ec9b8"),
        borderWidth=0.8,
        borderPadding=(5, 7, 5, 7),
        leading=13,
        spaceAfter=6,
    )
    interp_style = ParagraphStyle(
        "Interp",
        parent=styles["Normal"],
        fontSize=8.5,
        textColor=colors.HexColor("#2d3a4a"),
        leading=12,
        spaceAfter=4,
        alignment=TA_JUSTIFY,
    )
    small_style = ParagraphStyle(
        "Small",
        parent=styles["Normal"],
        fontSize=7.5,
        textColor=colors.HexColor("#5a7a8a"),
        leading=10,
        spaceAfter=3,
    )
    section_note_style = ParagraphStyle(
        "SectionNote",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#1a4060"),
        backColor=colors.HexColor("#e8f0fb"),
        borderColor=colors.HexColor("#8aaad8"),
        borderWidth=0.8,
        borderPadding=(4, 6, 4, 6),
        leading=12,
        spaceAfter=6,
    )
    cell_hdr_style = ParagraphStyle(
        "CellHdr",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.white,
        fontName="Helvetica-Bold",
        leading=11,
        spaceAfter=0,
    )
    cell_body_style = ParagraphStyle(
        "CellBody",
        parent=styles["Normal"],
        fontSize=7.5,
        textColor=colors.HexColor("#2d3a4a"),
        leading=10,
        spaceAfter=0,
    )
    cell_bold_style = ParagraphStyle(
        "CellBold",
        parent=styles["Normal"],
        fontSize=7.5,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#2d3a4a"),
        leading=10,
        spaceAfter=0,
    )

    story = []

    story.append(Paragraph("NI TB Genomic Surveillance", styles["Title"]))
    story.append(Paragraph("Outbreak Investigation Report", styles["Heading2"]))
    story.append(Paragraph(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC", styles["Normal"]))
    story.append(Spacer(1, 0.18 * inch))

    # ── About This Report ──────────────────────────────────────────────────────
    story.append(Paragraph("About This Report", styles["Heading3"]))
    story.append(Paragraph(
        "This report is produced by the Northern Ireland TB Genomic Surveillance platform using whole-genome sequencing (WGS) "
        "data and epidemiological case records. It is intended to support TB programme staff and public health investigators "
        "by providing genomic evidence for transmission clusters, drug-resistance profiles, and programme performance metrics. "
        "<b>This is a decision-support tool only — all findings must be reviewed and acted on by a qualified clinician or "
        "public health professional. No automated decisions are made.</b>",
        interp_style,
    ))
    story.append(Spacer(1, 0.1 * inch))

    # ── TB Genomics Background ─────────────────────────────────────────────────
    story.append(Paragraph("TB Genomics — Key Concepts", styles["Heading3"]))
    bg_rows = [
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
         Paragraph("A directed graph where arrows indicate the most probable direction of transmission. High-confidence links (posterior probability >0.70) "
                   "warrant immediate epidemiological follow-up to confirm exposure history.", cell_body_style)],
    ]
    bg_table = Table(bg_rows, colWidths=[1.8 * inch, 5.0 * inch])
    bg_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a5c4a")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4faf8")]),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#7ec9b8")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cce8e0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(bg_table)
    story.append(Paragraph(
        "Table 1. TB genomics reference — key terms used throughout this report.",
        caption_style,
    ))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Case Summary", styles["Heading3"]))
    summary_table_data = [
        ["Total Cases", str(int(total_cases))],
        ["Clustered Cases", str(int(clustered_cases))],
        ["Unclustered Cases", str(int(total_cases) - int(clustered_cases))],
        ["Open Clusters", str(int(open_clusters))],
    ]
    summary_table = Table(summary_table_data, colWidths=[2.4 * inch, 1.5 * inch])
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.append(summary_table)
    story.append(Paragraph(
        "Table 2. Programme case count summary. Clustered cases are those linked by genomic similarity to at least one other case. "
        "Unclustered (singleton) cases may represent imported strains, sporadic transmission, or reactivation of latent disease. "
        "Open clusters are active genomic transmission clusters with ongoing epidemiological investigation.",
        caption_style,
    ))
    story.append(Spacer(1, 0.15 * inch))

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
    if transmission_data and int(transmission_data.get("high_confidence_edges", 0) or 0) > 0:
        interpretation_flags.append(
            "High-confidence transmission links present; prioritize epidemiology follow-up."
        )

    story.append(Paragraph("Automated Interpretation Flags", styles["Heading3"]))
    if interpretation_flags:
        for flag in interpretation_flags:
            story.append(Paragraph(f"- {flag}", styles["Normal"]))
    else:
        story.append(Paragraph("No elevated operational risk flags detected in current report window.", styles["Normal"]))

    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Analysis Summary", styles["Heading3"]))
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
            analysis_table = Table(analysis_rows, colWidths=[2.4 * inch, 3.4 * inch])
            analysis_table.setStyle(
                TableStyle(
                    [
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                        ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ]
                )
            )
            story.append(analysis_table)
        else:
            story.append(Paragraph("No structured analysis metrics available.", styles["Normal"]))
    else:
        story.append(Paragraph("No outbreak summary JSON found.", styles["Normal"]))

    story.append(Paragraph(
        "Table 3. outbreaker2 Bayesian MCMC analysis parameters. "
        "<b>Posterior Samples</b> is the number of accepted MCMC draws used to compute estimates — higher values give more stable posteriors. "
        "<b>Mean Log-Likelihood</b> reflects model fit; values closer to zero (less negative) indicate better fit. "
        "<b>Transmission Probability</b> is the average posterior probability that any given case-pair represents a direct transmission event. "
        "<b>Generation Time</b> is the modelled average interval (in days) between infection events in a transmission chain. "
        "<b>Convergence Diagnostic</b> near 1.0 confirms the MCMC chain has stabilised; values >1.1 indicate cautious interpretation is needed.",
        caption_style,
    ))
    story.append(Paragraph(
        "<b>How to interpret outbreaker2 results:</b> outbreaker2 reconstructs the most probable transmission tree using "
        "both genetic distance (SNPs) and timing (collection dates). Pairs with high posterior transmission probability "
        "(>0.5) represent genomically and temporally plausible direct transmission events. These are candidates for "
        "epidemiological investigation to identify shared exposure. Lower probability pairs may still be linked "
        "within the same cluster but through one or more undetected intermediate cases.",
        section_note_style,
    ))

    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("Programme Surveillance KPIs (Last 12 Weeks)", styles["Heading3"]))
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
        kpi_table = Table(kpi_table_data, colWidths=[2.8 * inch, 3.0 * inch])
        kpi_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                ]
            )
        )
        story.append(kpi_table)

        if kpi_data.get("warning"):
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(f"KPI Warning: {kpi_data['warning']}", styles["Italic"]))

        story.append(Paragraph(
            "Table 4. Programme surveillance KPIs over the reporting window. "
            "<b>Sequencing Coverage</b> is the percentage of eligible TB culture-confirmed cases that have received whole-genome sequencing. "
            "The UK target is ≥80%. "
            "<b>QC Pass Rate</b> is the percentage of sequenced samples that meet quality thresholds (e.g. ≥95% genome coverage at ≥10×). "
            "Low pass rates may indicate DNA quality issues, contamination, or laboratory process variation. "
            "<b>Contamination Flags</b> indicate samples where a mixed-strain signal suggests cross-contamination requiring repeat or rejection.",
            caption_style,
        ))

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
            region_table = Table(region_rows, colWidths=[2.1 * inch, 1.1 * inch, 1.1 * inch, 1.1 * inch])
            region_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                        ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ]
                )
            )
            story.append(region_table)
    else:
        story.append(Paragraph("Surveillance KPIs unavailable.", styles["Normal"]))

    story.append(Paragraph(
        "Table 5. Regional sequencing representativeness. Regions with coverage <80% may introduce ascertainment bias — "
        "clusters in under-sequenced regions may be underdetected. Where persistent regional gaps exist, "
        "review laboratory submission pathways and specimen transport processes.",
        caption_style,
    ))
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("Weekly Surveillance Trends (12 Weeks)", styles["Heading3"]))
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
        trend_table = Table(trend_rows, colWidths=[1.35 * inch, 1.0 * inch, 1.0 * inch, 1.0 * inch, 1.0 * inch])
        trend_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(trend_table)

        trend_chart_path = build_trend_chart()
        if trend_chart_path and os.path.exists(trend_chart_path):
            story.append(Spacer(1, 0.12 * inch))
            story.append(build_report_image(trend_chart_path))
    else:
        story.append(Paragraph("Weekly trends unavailable.", styles["Normal"]))

    story.append(Paragraph(
        "Table 6. Weekly sequencing coverage and QC pass rates over the last 12 weeks. "
        "Figure 1 (below) plots coverage and QC trends — a declining trend may indicate emerging laboratory issues. "
        "Weeks with zero eligible cases may reflect reporting lags rather than true absence of TB.",
        caption_style,
    ))
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("Cluster Action Prioritization", styles["Heading3"]))
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
        action_table = Table(action_rows, colWidths=[1.1 * inch, 0.8 * inch, 0.85 * inch, 1.35 * inch, 1.0 * inch, 0.8 * inch])
        action_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(action_table)
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph(
            "Table 7. Clusters ranked by investigation priority score. Score is composite: cluster size (×2), "
            "cross-region spread (×3), specimen recency within 14/30/60 days (×3/2/1), open investigation status (×3). "
            "Higher scores indicate clusters warranting urgent epidemiological follow-up. "
            "<b>Status 'open'</b> means an active field investigation is ongoing or recommended.",
            caption_style,
        ))
        story.append(Paragraph(
            "<b>Recommended action:</b> For clusters with priority score >10 and status 'open', ensure field epidemiology is "
            "actively investigating shared exposure (household contacts, healthcare settings, social networks). "
            "Cross-region clusters may indicate transmission events during travel or care-seeking across NHS trust boundaries.",
            section_note_style,
        ))
    else:
        story.append(Paragraph("No cluster action data available.", styles["Normal"]))

    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("Lineage and Drug Resistance Validation", styles["Heading3"]))
    if lineage_dr_data:
        analysis_summary = _lineage_analysis_summary(db)
        lineage_engines = (lineage_dr_data.get("engines") or {})
        effective_engines = _derive_effective_engine_status(lineage_dr_data)
        lineage_rows = [
            ["Overall Status", str(lineage_dr_data.get("status", "unknown"))],
            ["TB-Profiler", str(effective_engines.get("tb_profiler", "unknown"))],
            ["Mykrobe", str(effective_engines.get("mykrobe", "unknown"))],
            ["Docker Fallback", str((lineage_dr_data.get("docker") or {}).get("fallback_enabled", False))],
            ["Docker Daemon Running", str((lineage_dr_data.get("docker") or {}).get("daemon_running", False))],
            ["FASTA Inputs", str((lineage_dr_data.get("inputs") or {}).get("fasta_count", 0))],
            ["Interpreted Samples", str(analysis_summary.get("interpreted_samples", 0))],
            ["Samples with Lineage", str(analysis_summary.get("samples_with_lineage", 0))],
            ["Samples with Resistance Calls", str(analysis_summary.get("samples_with_resistance_calls", 0))],
            ["TB-Profiler (Local)", str((lineage_engines.get("tb_profiler") or {}).get("status", "unknown"))],
            ["Mykrobe (Local)", str((lineage_engines.get("mykrobe") or {}).get("status", "unknown"))],
        ]
        lineage_table = Table(lineage_rows, colWidths=[2.8 * inch, 3.0 * inch])
        lineage_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                ]
            )
        )
        story.append(lineage_table)

        story.append(Paragraph(
            "Table 8. Lineage and drug-resistance validation status. TB-Profiler and Mykrobe are bioinformatic pipelines "
            "that classify M. tuberculosis lineage and predict drug resistance from WGS reads. "
            "'Available' means the tool executed successfully; 'unavailable' may indicate missing software, Docker daemon issues, or insufficient FASTA inputs. "
            "FASTA inputs refers to the number of consensus genome sequences submitted for analysis.",
            caption_style,
        ))
        next_steps = lineage_dr_data.get("next_steps") or []
        if next_steps:
            story.append(Spacer(1, 0.08 * inch))
            story.append(Paragraph("Lineage/DR Next Steps", styles["Heading4"]))
            for step in next_steps[:4]:
                story.append(Paragraph(f"- {str(step)}", styles["Normal"]))
    else:
        story.append(Paragraph("No lineage/DR validation artifact found.", styles["Normal"]))

    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("Secondary Transmission Engines", styles["Heading3"]))
    if secondary_validation_data:
        secondary_engines = secondary_validation_data.get("engines") or {}
        transphylo_state = str((secondary_engines.get("transphylo") or {}).get("status", "unknown"))
        bactdating_state = str((secondary_engines.get("bactdating") or {}).get("status", "unknown"))
        secondary_rows = [
            ["Secondary Validation Status", str(secondary_validation_data.get("status", "unknown"))],
            ["Consensus State", str((secondary_validation_data.get("consensus") or {}).get("status", "unknown"))],
            ["TransPhylo", transphylo_state],
            ["BactDating", bactdating_state],
            ["Tree Input Available", str((secondary_validation_data.get("prerequisites") or {}).get("has_tree_newick", False))],
            ["Cases Input Available", str((secondary_validation_data.get("prerequisites") or {}).get("has_cases_csv", False))],
        ]
        secondary_table = Table(secondary_rows, colWidths=[2.8 * inch, 3.0 * inch])
        secondary_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                ]
            )
        )
        story.append(secondary_table)
        story.append(Paragraph(
            "Table 9. Secondary engine validation. TransPhylo uses a phylogenetic tree and sampling dates to reconstruct "
            "transmission under a within-host evolutionary model. BactDating estimates dated ancestral phylogenies to calibrate "
            "transmission timelines. Both require a Newick-format phylogenetic tree as input. "
            "Where tools are unavailable, outbreaker2 results remain the primary genomic evidence.",
            caption_style,
        ))
    else:
        story.append(Paragraph("No secondary engine validation artifact found.", styles["Normal"]))

    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("Cross-Method Clustering Comparison", styles["Heading3"]))
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
        comp_table = Table(comp_rows, colWidths=[2.8 * inch, 3.0 * inch])
        comp_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                ]
            )
        )
        story.append(comp_table)
    else:
        story.append(Paragraph("No cluster method comparison artifact found.", styles["Normal"]))

    story.append(Paragraph(
        "Table 10. Agreement between SNP-threshold sequence clustering and outbreaker2 probabilistic clustering. "
        "<b>Precision</b>: of pairs grouped together by outbreaker2, the fraction also grouped by sequence clusters. "
        "<b>Recall</b>: of pairs grouped by sequence clusters, the fraction also grouped by outbreaker2. "
        "<b>Jaccard</b>: overall overlap index (0=no agreement, 1=perfect agreement). "
        "Discordant pairs — grouped by one method but not the other — may represent cases where temporal data "
        "(outbreaker2) overrides genomic distance alone, or where the SNP threshold is set differently.",
        caption_style,
    ))

    if sequence_summary_data:
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph("Sequence Clustering Snapshot", styles["Heading4"]))
        seq_rows = []
        for key in ["total_sequences", "assigned_sequences", "cluster_count", "largest_cluster_size", "singleton_count"]:
            if key in sequence_summary_data:
                seq_rows.append([key.replace("_", " ").title(), str(sequence_summary_data.get(key))])
        if seq_rows:
            seq_table = Table(seq_rows, colWidths=[2.8 * inch, 3.0 * inch])
            seq_table.setStyle(
                TableStyle(
                    [
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                        ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ]
                )
            )
            story.append(seq_table)

    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("Transmission Priority Signals", styles["Heading3"]))
    # ── TB Transmission Routes — Background ───────────────────────────────────
    story.append(Paragraph("Understanding TB Transmission Routes from WGS", styles["Heading3"]))
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
    routes_table = Table(routes_rows, colWidths=[1.55 * inch, 0.8 * inch, 2.3 * inch, 2.15 * inch])
    routes_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2a5080")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4fa")]),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#8aaad8")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c8d8ec")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(routes_table)
    story.append(Paragraph(
        "Table 11. TB transmission route classification by SNP distance (M. tuberculosis whole-genome comparison). "
        "SNP thresholds follow UK NICE guideline NG33 and published literature (Walker et al. 2013, Meehan et al. 2019). "
        "The generation time assumed in outbreaker2 modelling is set at programme configuration and affects when "
        "a given SNP distance is interpreted as consistent with direct vs indirect transmission.",
        caption_style,
    ))
    story.append(Spacer(1, 0.18 * inch))

    story.append(Paragraph("Transmission Priority Signals", styles["Heading3"]))
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
        priority_table = Table(priority_rows, colWidths=[1.25 * inch, 1.9 * inch, 1.0 * inch, 0.8 * inch, 0.8 * inch])
        priority_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                ]
            )
        )
        story.append(priority_table)

        story.append(Paragraph(
            f"Table 12. Top-priority cases by network centrality. "
            f"Network snapshot: {transmission_data.get('node_count', 0)} nodes, "
            f"{transmission_data.get('edge_count', 0)} directed links, "
            f"{transmission_data.get('high_confidence_edges', 0)} high-confidence links (posterior >0.70). "
            "<b>Out</b> = outgoing transmission links (potential sources); <b>In</b> = incoming links (potential recipients). "
            "Cases with multiple outgoing high-confidence links ('superspreaders') should be prioritised for epidemiological investigation.",
            caption_style,
        ))
        story.append(Paragraph(
            "<b>High-confidence transmission links</b> (posterior probability >0.70) represent the strongest genomic evidence "
            "of direct transmission between a pair of cases. Investigators should review the contact history for all such pairs. "
            "Where epidemiological linkage can be confirmed, the direction of transmission (source → recipient) inferred by "
            "outbreaker2 can inform contact prioritisation for LTBI screening.",
            section_note_style,
        ))
    else:
        story.append(Paragraph("No transmission priority data found.", styles["Normal"]))

    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("Diagnostic Graphics", styles["Heading3"]))
    story.append(Paragraph(
        "The following plots are generated by outbreaker2 and the platform's supplementary visualisation pipeline. "
        "Each figure caption explains the content and how to interpret the output.",
        interp_style,
    ))
    story.append(Spacer(1, 0.08 * inch))

    FIGURE_CAPTIONS = {
        "outbreaker_trace.png": (
            "Figure 2. MCMC trace plot — log-posterior probability over iterations. "
            "A well-mixed chain shows stable fluctuation around a mean value (no upward/downward drift). "
            "If the trace shows a long burn-in slope or multiple plateaux, the chain may not have converged; "
            "consider increasing iterations or checking input data quality."
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
            image_path = os.path.join("exports", name)
            story.append(build_report_image(image_path))
            caption_text = FIGURE_CAPTIONS.get(name)
            if not caption_text:
                label = name.replace("outbreaker_", "").replace(".png", "").replace("_", " ").title()
                caption_text = f"Figure. {label} — generated by the outbreaker2 analysis pipeline."
            story.append(Paragraph(caption_text, caption_style))
            story.append(Spacer(1, 0.15 * inch))
    else:
        story.append(Paragraph("No outbreak graphics found in exports/.", styles["Normal"]))

    # ── Lineage Clinical Reference ─────────────────────────────────────────────
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("M. tuberculosis Lineage Reference", styles["Heading3"]))
    story.append(Paragraph(
        "Lineage classification places a strain within the global phylogeny of M. tuberculosis. Lineage influences "
        "drug-resistance acquisition patterns and geographic origin. The following table provides clinical and "
        "epidemiological context for lineages commonly observed in Northern Ireland.",
        interp_style,
    ))
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
    lin_table = Table(lineage_ref_rows, colWidths=[0.55 * inch, 1.35 * inch, 1.3 * inch, 1.4 * inch, 2.2 * inch])
    lin_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a5c4a")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4faf8")]),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#7ec9b8")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cce8e0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(lin_table)
    story.append(Paragraph(
        "Table 13. M. tuberculosis lineage reference for clinical and epidemiological context. "
        "DR = drug resistance; MDR = multidrug-resistant; XDR = extensively drug-resistant. "
        "Lineage assignment should be combined with phenotypic DST and clinical judgment. "
        "Source: Coll et al. (2014) Nature Genetics; WHO Global TB Report 2023.",
        caption_style,
    ))

    # ── Clinical Action Summary ────────────────────────────────────────────────
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("Clinical and Public Health Action Summary", styles["Heading3"]))
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
        [Paragraph("High-confidence transmission link identified (posterior >0.70)", cell_bold_style),
         Paragraph("Epidemiological review of both cases; document shared exposure if found; "
                   "update cluster investigation record.", cell_body_style),
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
    act_table = Table(action_ref_rows, colWidths=[2.0 * inch, 3.8 * inch, 1.0 * inch])
    act_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2a5080")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4fa")]),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#8aaad8")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c8d8ec")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(act_table)
    story.append(Paragraph(
        "Table 14. Recommended clinical and public health actions mapped to genomic findings. "
        "Urgency thresholds align with PHE/PHA TB operational guidance. "
        "All genomic findings must be reviewed in conjunction with clinical history, contact tracing records, "
        "and microbiological DST before action is taken.",
        caption_style,
    ))
    story.append(Paragraph(
        "<b>Disclaimer:</b> Genomic cluster assignments and transmission inferences are probabilistic estimates "
        "based on mathematical models. They supplement but do not replace epidemiological investigation. "
        "Do not use genomic evidence alone to assign legal or clinical liability for transmission.",
        small_style,
    ))

    story.append(Spacer(1, 0.16 * inch))
    story.append(Paragraph("Data Provenance", styles["Heading3"]))
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

    output_path = report_path
    try:
        doc.build(story)
    except PermissionError:
        # If the default report file is open/locked (common on Windows),
        # generate a timestamped filename so report creation still succeeds.
        stamped_name = f"outbreaker_investigation_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
        output_path = os.path.join("exports", stamped_name)
        doc = SimpleDocTemplate(output_path, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
        doc.build(story)

    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename=os.path.basename(output_path),
    )


@router.get("/outbreaker-image/{filename}")
def get_outbreaker_image(filename: str):
    """Serve outbreaker2 generated graphics."""
    path = f"exports/{filename}"
    
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
