import csv
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from itertools import combinations

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from backend.database import SessionLocal


def _snp_distance(seq_a: str, seq_b: str) -> int:
    if not seq_a or not seq_b:
        return 10**9
    a = seq_a.upper()
    b = seq_b.upper()
    common = min(len(a), len(b))
    mismatches = sum(1 for i in range(common) if a[i] != b[i])
    return mismatches + abs(len(a) - len(b))


class UnionFind:
    def __init__(self, items):
        self.parent = {item: item for item in items}
        self.rank = {item: 0 for item in items}

    def find(self, item):
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, a, b):
        root_a = self.find(a)
        root_b = self.find(b)
        if root_a == root_b:
            return
        rank_a = self.rank[root_a]
        rank_b = self.rank[root_b]
        if rank_a < rank_b:
            self.parent[root_a] = root_b
        elif rank_a > rank_b:
            self.parent[root_b] = root_a
        else:
            self.parent[root_b] = root_a
            self.rank[root_a] += 1


def main() -> None:
    os.makedirs("exports", exist_ok=True)
    threshold = int(os.getenv("SEQ_CLUSTER_MAX_DISTANCE", "25"))
    min_cluster_size = int(os.getenv("SEQ_CLUSTER_MIN_SIZE", "2"))

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
                SELECT
                    c.pseudonymised_case_id::text AS case_id,
                    c.specimen_date,
                    c.geographic_region,
                    cs.sequence
                FROM consensus_sequences cs
                JOIN cases c ON c.pseudonymised_case_id = cs.sample_id
                ORDER BY c.specimen_date ASC NULLS LAST
                """
            )
        ).mappings().all()

        samples = [
            {
                "case_id": row["case_id"],
                "specimen_date": row["specimen_date"],
                "geographic_region": row["geographic_region"],
                "sequence": (row["sequence"] or "").strip().upper(),
            }
            for row in rows
            if row.get("sequence")
        ]

        # Replace current cluster assignment with sequence-derived clusters.
        # cluster_investigations holds a direct FK to clusters, and TRUNCATE
        # requires all referenced tables to be cleared in the same statement.
        db.execute(text("TRUNCATE TABLE case_clusters, cluster_investigations, clusters"))

        summary = {
            "status": "ok",
            "method": "sequence_distance_connected_components",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "threshold_snp_distance": threshold,
            "min_cluster_size": min_cluster_size,
            "sequenced_cases": len(samples),
            "clustered_cases": 0,
            "cluster_count": 0,
            "unclustered_cases": len(samples),
            "pairwise_links_under_threshold": 0,
            "clusters": [],
        }

        if len(samples) < 2:
            with open("exports/sequence_clustering_summary.json", "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2)
            with open("exports/sequence_cluster_assignments.csv", "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=["case_id", "specimen_date", "geographic_region", "cluster_id", "source"],
                )
                writer.writeheader()
            db.commit()
            print(json.dumps(summary, indent=2))
            return

        uf = UnionFind([s["case_id"] for s in samples])
        sample_map = {s["case_id"]: s for s in samples}

        pairwise_links = 0
        for a_id, b_id in combinations(sample_map.keys(), 2):
            dist = _snp_distance(sample_map[a_id]["sequence"], sample_map[b_id]["sequence"])
            if dist <= threshold:
                uf.union(a_id, b_id)
                pairwise_links += 1

        groups = {}
        for case_id in sample_map.keys():
            root = uf.find(case_id)
            groups.setdefault(root, []).append(case_id)

        cluster_components = [sorted(members) for members in groups.values() if len(members) >= min_cluster_size]

        assignments = []
        for idx, members in enumerate(sorted(cluster_components, key=len, reverse=True), start=1):
            namespace_key = "seqcluster:" + "|".join(members)
            cluster_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, namespace_key))

            db.execute(
                text(
                    "INSERT INTO clusters (cluster_id, snp_distance, investigation_status, alert_flag) "
                    "VALUES (:cluster_id, :snp_distance, :investigation_status, :alert_flag)"
                ),
                {
                    "cluster_id": cluster_uuid,
                    "snp_distance": threshold,
                    "investigation_status": "open",
                    "alert_flag": len(members) >= 5,
                },
            )

            for case_id in members:
                db.execute(
                    text("INSERT INTO case_clusters (sample_id, cluster_id) VALUES (CAST(:sample_id AS uuid), CAST(:cluster_id AS uuid))"),
                    {"sample_id": case_id, "cluster_id": cluster_uuid},
                )
                s = sample_map[case_id]
                assignments.append(
                    {
                        "case_id": case_id,
                        "specimen_date": str(s.get("specimen_date") or ""),
                        "geographic_region": s.get("geographic_region") or "",
                        "cluster_id": cluster_uuid,
                        "source": "sequence_derived",
                    }
                )

            summary["clusters"].append(
                {
                    "cluster_label": f"SEQ-{idx:03d}",
                    "cluster_id": cluster_uuid,
                    "case_count": len(members),
                }
            )

        summary["pairwise_links_under_threshold"] = pairwise_links
        summary["cluster_count"] = len(summary["clusters"])
        summary["clustered_cases"] = sum(c["case_count"] for c in summary["clusters"])
        summary["unclustered_cases"] = len(samples) - summary["clustered_cases"]

        with open("exports/sequence_clustering_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        with open("exports/sequence_cluster_assignments.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["case_id", "specimen_date", "geographic_region", "cluster_id", "source"],
            )
            writer.writeheader()
            for row in assignments:
                writer.writerow(row)

        db.execute(
            text(
                "INSERT INTO audit_log (action, user_id, details, timestamp) "
                "VALUES (:action, :user_id, CAST(:details AS jsonb), NOW())"
            ),
            {
                "action": "derive_sequence_clusters",
                "user_id": "system",
                "details": json.dumps(summary),
            },
        )

        db.commit()
        print(json.dumps(summary, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
