
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
                    WHERE specimen_date >= CURRENT_DATE - (:weeks::int * INTERVAL '7 days')
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
                WHERE specimen_date >= CURRENT_DATE - (:weeks::int * INTERVAL '7 days')
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


@router.get("/outbreaker-status")
def outbreaker_status():
    return {
        "cases_export": os.path.exists("exports/cases.csv"),
        "dna_export": os.path.exists("exports/dna.fasta"),
        "results_rds": os.path.exists("outbreaker2_results.rds"),
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
    }
    
    # Check for analysis summary
    summary_path = "exports/outbreaker_summary.json"
    if os.path.exists(summary_path):
        try:
            with open(summary_path, "r") as f:
                result["summary"] = json.load(f)
                result["status"] = "completed"
        except Exception as e:
            result["status"] = "error"
            result["message"] = str(e)

    network_path = "exports/transmission_network.json"
    if os.path.exists(network_path):
        try:
            with open(network_path, "r", encoding="utf-8") as f:
                result["transmission_network"] = json.load(f)
        except Exception:
            result["transmission_network"] = None
    
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

    story.append(Paragraph("Analysis Summary", styles["Heading3"]))
    if summary_data:
        analysis_rows = []
        for key in ["n_samples", "n_iter", "burnin", "likelihood_mean", "converged"]:
            if key in summary_data:
                analysis_rows.append([key, str(summary_data[key])])
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
    story.append(Paragraph("Diagnostic Graphics", styles["Heading3"]))
    if existing_graphics:
        for name in existing_graphics:
            story.append(Paragraph(name.replace("outbreaker_", "").replace(".png", "").title(), styles["Heading4"]))
            image_path = os.path.join("exports", name)
            story.append(build_report_image(image_path))
            story.append(Spacer(1, 0.12 * inch))
    else:
        story.append(Paragraph("No outbreak graphics found in exports/.", styles["Normal"]))

    doc.build(story)

    return FileResponse(
        report_path,
        media_type="application/pdf",
        filename="outbreaker_investigation_report.pdf",
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
