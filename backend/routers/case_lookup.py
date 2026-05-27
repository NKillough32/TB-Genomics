from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.routers.case_overview import get_db


router = APIRouter(prefix="/cases", tags=["cases"])


@router.get("/search")
def advanced_search(
    region: str = None,
    lineage: str = None,
    date_from: str = None,
    date_to: str = None,
    resistance: str = None,
    cluster_id: str = None,
    db: Session = Depends(get_db),
):
    """Advanced search with multiple filter options."""
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
        WHERE COALESCE(c.entered_in_error, false) = false
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
            "cluster_id": cluster_id,
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
                "cluster_size": row["cluster_size"] if row["cluster_id"] else 0,
            }
            for row in results
        ],
    }


@router.get("/case-history/{case_id}")
def get_case_history(case_id: str, db: Session = Depends(get_db)):
    """Get case history: related samples and follow-ups grouped by region/case."""
    case_id_pattern = f"{case_id}%" if len(case_id) < 36 else case_id

    case_result = db.execute(
        text(
            """
            SELECT pseudonymised_case_id, geographic_region FROM cases
            WHERE CAST(pseudonymised_case_id AS TEXT) LIKE :case_id_pattern
              AND COALESCE(entered_in_error, false) = false
            LIMIT 1
            """
        ),
        {"case_id_pattern": case_id_pattern},
    ).mappings().first()

    if not case_result:
        return {"error": "Case not found", "case_id": case_id}

    full_case_id = case_result["pseudonymised_case_id"]
    region = case_result["geographic_region"]

    results = db.execute(
        text(
            """
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
            WHERE (c.pseudonymised_case_id = :case_id OR c.geographic_region = :region)
              AND COALESCE(c.entered_in_error, false) = false
            ORDER BY c.specimen_date
            """
        ),
        {"case_id": full_case_id, "region": region},
    ).mappings().all()

    if not results:
        return {"error": "Case not found", "case_id": case_id}

    case_history = [
        {
            "case_id": str(row["pseudonymised_case_id"])[:8],
            "specimen_date": str(row["specimen_date"]),
            "region": row["geographic_region"],
            "status": row["case_status"],
            "lineage": row["lineage"],
            "resistance": row["predicted_drug_resistance"],
            "cluster_id": str(row["cluster_id"])[:8] if row["cluster_id"] else None,
            "is_index_case": str(row["pseudonymised_case_id"]) == str(full_case_id),
        }
        for row in results
    ]

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
        "history": case_history,
    }

