import csv
import json
import os
import sys
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from backend.database import SessionLocal


def _write_json_with_fallback(preferred_path: str, payload: dict) -> str:
    try:
        with open(preferred_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return preferred_path
    except PermissionError:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        root, ext = os.path.splitext(preferred_path)
        fallback_path = f"{root}_{stamp}{ext}"
        with open(fallback_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return fallback_path


def _write_csv_with_fallback(preferred_path: str, rows: list[dict]) -> str:
    fieldnames = ["case_id", "specimen_date", "geographic_region", "cluster_id"]
    try:
        with open(preferred_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return preferred_path
    except PermissionError:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        root, ext = os.path.splitext(preferred_path)
        fallback_path = f"{root}_{stamp}{ext}"
        with open(fallback_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return fallback_path


def main() -> None:
    os.makedirs("exports", exist_ok=True)
    db = SessionLocal()

    try:
        total_cases = db.execute(text("SELECT COUNT(*) FROM cases")).scalar() or 0
        linked_cases = db.execute(text("SELECT COUNT(DISTINCT sample_id) FROM case_clusters")).scalar() or 0
        cluster_count = db.execute(text("SELECT COUNT(*) FROM clusters")).scalar() or 0

        top_clusters = db.execute(
            text(
                """
                SELECT cc.cluster_id::text AS cluster_id, COUNT(*) AS case_count
                FROM case_clusters cc
                GROUP BY cc.cluster_id
                ORDER BY case_count DESC
                LIMIT 10
                """
            )
        ).mappings().all()

        assignments = db.execute(
            text(
                """
                SELECT c.pseudonymised_case_id::text AS case_id,
                       c.specimen_date,
                       c.geographic_region,
                       cc.cluster_id::text AS cluster_id
                FROM cases c
                LEFT JOIN case_clusters cc
                    ON cc.sample_id = c.pseudonymised_case_id
                ORDER BY c.specimen_date DESC NULLS LAST
                """
            )
        ).mappings().all()

        summary = {
            "status": "ok",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_cases": int(total_cases),
            "clustered_cases": int(linked_cases),
            "unclustered_cases": int(total_cases) - int(linked_cases),
            "cluster_count": int(cluster_count),
            "top_clusters": [
                {"cluster_id": row["cluster_id"], "case_count": int(row["case_count"])}
                for row in top_clusters
            ],
        }

        summary_path = _write_json_with_fallback("exports/clustering_summary.json", summary)

        assignment_rows = [
            {
                "case_id": row["case_id"],
                "specimen_date": row["specimen_date"],
                "geographic_region": row["geographic_region"],
                "cluster_id": row["cluster_id"] or "",
            }
            for row in assignments
        ]
        assignments_path = _write_csv_with_fallback("exports/cluster_assignments.csv", assignment_rows)

        summary["output_files"] = {
            "summary_json": summary_path,
            "assignments_csv": assignments_path,
        }

        print(json.dumps(summary, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
