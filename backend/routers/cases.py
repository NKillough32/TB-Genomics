
import os
import json

from datetime import datetime
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from backend.database import SessionLocal
from backend.models import Case

router = APIRouter(prefix="/cases", tags=["cases"])

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

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
def lineage_dr_validation():
    """Return lineage/drug-resistance integration validation artifact."""
    path = "exports/lineage_dr_validation.json"
    if not os.path.exists(path):
        return {
            "status": "no_results",
            "message": "Lineage/DR validation has not been run yet",
            "artifact_path": path,
        }

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
            "artifact_path": path,
        }

    payload["artifact_path"] = path
    return payload


@router.get("/outbreak-report")
def outbreak_report(db: Session = Depends(get_db)):
    """Generate and return a PDF outbreak investigation report."""
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
    story = []

    story.append(Paragraph("NI TB Genomic Surveillance", styles["Title"]))
    story.append(Paragraph("Outbreak Investigation Report", styles["Heading2"]))
    story.append(Paragraph(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC", styles["Normal"]))
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
    story.append(Spacer(1, 0.2 * inch))

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
        story.append(
            Paragraph(
                "Priority score combines cluster size, cross-region spread, specimen recency, and open investigation status.",
                styles["Normal"],
            )
        )
    else:
        story.append(Paragraph("No cluster action data available.", styles["Normal"]))

    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("Lineage and Drug Resistance Validation", styles["Heading3"]))
    if lineage_dr_data:
        lineage_engines = (lineage_dr_data.get("engines") or {})
        lineage_rows = [
            ["Overall Status", str(lineage_dr_data.get("status", "unknown"))],
            ["TB-Profiler", str((lineage_engines.get("tb_profiler") or {}).get("status", "unknown"))],
            ["Mykrobe", str((lineage_engines.get("mykrobe") or {}).get("status", "unknown"))],
            ["Docker Fallback", str((lineage_dr_data.get("docker") or {}).get("fallback_enabled", False))],
            ["Docker Daemon Running", str((lineage_dr_data.get("docker") or {}).get("daemon_running", False))],
            ["FASTA Inputs", str((lineage_dr_data.get("inputs") or {}).get("fasta_count", 0))],
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

        story.append(Spacer(1, 0.1 * inch))
        story.append(
            Paragraph(
                (
                    f"Network snapshot: nodes={transmission_data.get('node_count', 0)}, "
                    f"edges={transmission_data.get('edge_count', 0)}, "
                    f"high-confidence links={transmission_data.get('high_confidence_edges', 0)}"
                ),
                styles["Normal"],
            )
        )
    else:
        story.append(Paragraph("No transmission priority data found.", styles["Normal"]))

    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("Diagnostic Graphics", styles["Heading3"]))
    if existing_graphics:
        for name in existing_graphics:
            story.append(Paragraph(name.replace("outbreaker_", "").replace(".png", "").title(), styles["Heading4"]))
            image_path = os.path.join("exports", name)
            story.append(build_report_image(image_path))
            story.append(Spacer(1, 0.12 * inch))
    else:
        story.append(Paragraph("No outbreak graphics found in exports/.", styles["Normal"]))

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
