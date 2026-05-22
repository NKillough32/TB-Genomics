#!/usr/bin/env python3
"""Generate operational surveillance alerts from current database evidence."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from backend.database import SessionLocal
from backend.runtime_paths import EXPORTS_DIR
from backend.snp_validation import validated_snp_distance


def _insert_alert(db, *, alert_type: str, severity: str, sample_id: str | None, cluster_id: str | None, title: str, description: str, evidence: dict) -> bool:
    existing = db.execute(
        text(
            """
            SELECT alert_id
            FROM alerts
            WHERE status IN ('open', 'acknowledged')
              AND alert_type = :alert_type
              AND COALESCE(sample_id::text, '') = COALESCE(:sample_id, '')
              AND COALESCE(cluster_id::text, '') = COALESCE(:cluster_id, '')
              AND title = :title
            LIMIT 1
            """
        ),
        {"alert_type": alert_type, "sample_id": sample_id, "cluster_id": cluster_id, "title": title},
    ).first()
    if existing:
        return False

    db.execute(
        text(
            """
            INSERT INTO alerts (
              alert_type, severity, sample_id, cluster_id, title, description, evidence, created_at, updated_at
            )
            VALUES (
              :alert_type, :severity, CAST(:sample_id AS uuid), CAST(:cluster_id AS uuid),
              :title, :description, CAST(:evidence AS jsonb), NOW(), NOW()
            )
            """
        ),
        {
            "alert_type": alert_type,
            "severity": severity,
            "sample_id": sample_id,
            "cluster_id": cluster_id,
            "title": title,
            "description": description,
            "evidence": json.dumps(evidence),
        },
    )
    return True


def _sequence_rows(db):
    return db.execute(
        text(
            """
            SELECT c.pseudonymised_case_id::text AS case_id,
                   c.geographic_region,
                   cs.sequence
            FROM consensus_sequences cs
            JOIN cases c ON c.pseudonymised_case_id = cs.sample_id
            WHERE cs.sequence IS NOT NULL
            """
        )
    ).mappings().all()


def generate_alerts() -> dict:
    db = SessionLocal()
    created = 0
    try:
        probable_threshold = int(os.getenv("TB_ALERT_PROBABLE_SNP", "5"))
        possible_threshold = int(os.getenv("TB_ALERT_POSSIBLE_SNP", "12"))

        samples = [
            {
                "case_id": str(row["case_id"]),
                "region": row["geographic_region"],
                "sequence": str(row["sequence"] or "").strip().upper(),
            }
            for row in _sequence_rows(db)
            if row.get("sequence")
        ]
        for i, left in enumerate(samples):
            for right in samples[i + 1 :]:
                dist = validated_snp_distance(left["sequence"], right["sequence"]).distance
                if dist <= probable_threshold:
                    severity = "high"
                    alert_type = "probable_cluster"
                    label = "probable"
                elif dist <= possible_threshold:
                    severity = "medium"
                    alert_type = "possible_cluster"
                    label = "possible"
                else:
                    continue
                created += int(
                    _insert_alert(
                        db,
                        alert_type=alert_type,
                        severity=severity,
                        sample_id=right["case_id"],
                        cluster_id=None,
                        title=f"{label.title()} cluster link: {left['case_id'][:8]} / {right['case_id'][:8]}",
                        description=f"Pairwise SNP distance {dist} is within the {label} cluster alert threshold.",
                        evidence={"source": "snp_distance", "case_a": left["case_id"], "case_b": right["case_id"], "snp_distance": dist},
                    )
                )

        resistant_rows = db.execute(
            text(
                """
                SELECT sample_id::text AS sample_id, drug, gene, mutation, prediction, database_version
                FROM resistance_calls
                WHERE UPPER(COALESCE(prediction, '')) IN ('R', 'RESISTANT')
                   OR LOWER(COALESCE(prediction, '')) LIKE '%resistant%'
                """
            )
        ).mappings().all()
        for row in resistant_rows:
            drug = str(row["drug"] or "").lower()
            severity = "critical" if drug in {"rifampicin", "rifampin", "isoniazid"} else "high"
            alert_type = "resistance_alert" if severity == "high" else "mdr_rr_tb_alert"
            created += int(
                _insert_alert(
                    db,
                    alert_type=alert_type,
                    severity=severity,
                    sample_id=row["sample_id"],
                    cluster_id=None,
                    title=f"Resistance alert: {str(row['sample_id'])[:8]} {row['drug']}",
                    description="TB-Profiler-derived normalized resistance call requires public-health review.",
                    evidence=dict(row),
                )
            )

        setting_rows = db.execute(
            text(
                """
                SELECT DISTINCT cc.cluster_id::text AS cluster_id, l.location_type
                FROM case_clusters cc
                JOIN case_location_events cle ON cle.case_id = cc.sample_id
                JOIN locations l ON l.location_id = cle.location_id
                WHERE LOWER(COALESCE(l.location_type, '')) IN ('prison', 'hospital', 'homeless_hostel', 'hostel')
                """
            )
        ).mappings().all()
        for row in setting_rows:
            created += int(
                _insert_alert(
                    db,
                    alert_type="setting_alert",
                    severity="high",
                    sample_id=None,
                    cluster_id=row["cluster_id"],
                    title=f"High-risk setting cluster: {row['location_type']}",
                    description="Cluster includes a prison, hospital, or homeless-hostel exposure location.",
                    evidence=dict(row),
                )
            )

        regional_rows = db.execute(
            text(
                """
                SELECT cc.cluster_id::text AS cluster_id,
                       COUNT(DISTINCT c.geographic_region) AS region_count,
                       ARRAY_AGG(DISTINCT c.geographic_region) AS regions
                FROM case_clusters cc
                JOIN cases c ON c.pseudonymised_case_id = cc.sample_id
                GROUP BY cc.cluster_id
                HAVING COUNT(DISTINCT c.geographic_region) > 1
                """
            )
        ).mappings().all()
        for row in regional_rows:
            created += int(
                _insert_alert(
                    db,
                    alert_type="regional_alert",
                    severity="medium",
                    sample_id=None,
                    cluster_id=row["cluster_id"],
                    title=f"Cross-region cluster expansion: {str(row['cluster_id'])[:8]}",
                    description="Cluster contains cases from more than one geographic region/trust.",
                    evidence={"cluster_id": row["cluster_id"], "regions": list(row["regions"] or [])},
                )
            )

        db.execute(
            text(
                "INSERT INTO audit_log (action, user_id, details, timestamp) VALUES (:action, :user_id, CAST(:details AS jsonb), NOW())"
            ),
            {"action": "generate_alerts", "user_id": "system", "details": json.dumps({"created_alerts": created})},
        )
        db.commit()
        result = {"status": "completed", "created_alerts": created}
        try:
            EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
            (EXPORTS_DIR / "alert_generation_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        except Exception:
            pass
        return result
    finally:
        db.close()


def main() -> int:
    print(json.dumps(generate_alerts(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
