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
from backend.snp_validation import validated_snp_distance


def _snp_distance(seq_a: str, seq_b: str) -> tuple[int, int, int, str]:
    result = validated_snp_distance(seq_a, seq_b)
    return result.distance, result.comparable_sites, result.ambiguous_sites, result.status


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
    
    # Print startup diagnostics
    print("=" * 70)
    print("DERIVE SEQUENCE CLUSTERS - START")
    print("=" * 70)
    
    threshold = int(os.getenv("SEQ_CLUSTER_MAX_DISTANCE", "25"))
    min_cluster_size = int(os.getenv("SEQ_CLUSTER_MIN_SIZE", "2"))
    alert_min_cluster_size = int(os.getenv("TB_CLUSTER_ALERT_MIN_CASES", "5"))
    max_sequences = int(os.getenv("SEQ_CLUSTER_MAX_SEQUENCES", "0"))  # 0 = no limit
    
    print(f"Configuration:")
    print(f"  SEQ_CLUSTER_MAX_DISTANCE: {threshold}")
    print(f"  SEQ_CLUSTER_MIN_SIZE: {min_cluster_size}")
    print(f"  TB_CLUSTER_ALERT_MIN_CASES: {alert_min_cluster_size}")
    print(f"  SEQ_CLUSTER_MAX_SEQUENCES: {max_sequences}")

    db = SessionLocal()
    try:
        print("\nConnecting to database...")
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
        
        print(f"Loaded {len(rows)} sequence rows from database")

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

        # Scale guard: limit pairwise comparisons to avoid timeouts
        if max_sequences > 0 and len(samples) > max_sequences:
            print(f"WARNING: {len(samples)} sequences loaded but SEQ_CLUSTER_MAX_SEQUENCES={max_sequences}. "
                  f"Clustering will be limited to first {max_sequences} samples. "
                  f"To process all, set SEQ_CLUSTER_MAX_SEQUENCES to 0 or higher value.")
            samples = samples[:max_sequences]
        
        print(f"Processing {len(samples)} valid sequences")

        # Preserve investigation records across re-runs using a diff-based approach.
        # Instead of TRUNCATE (which destroys assignee, notes, actions, risk band overrides),
        # we load the existing cluster assignments, compare by membership, and only create
        # new cluster_investigations rows for genuinely new clusters. Unchanged clusters
        # (same UUID derived from same members) keep all their investigation history.

        summary = {
            "status": "ok",
            "method": "sequence_distance_connected_components",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "threshold_snp_distance": threshold,
            "min_cluster_size": min_cluster_size,
            "alert_min_cluster_size": alert_min_cluster_size,
            "sequenced_cases": len(samples),
            "clustered_cases": 0,
            "cluster_count": 0,
            "unclustered_cases": len(samples),
            "pairwise_links_under_threshold": 0,
            "pairwise_comparable_sites": {
                "mean": None,
                "min": None,
                "max": None,
            },
            "link_pair_comparable_sites": {
                "mean": None,
                "min": None,
                "max": None,
            },
            "pairwise_snp_distance_histogram": {},
            "clusters": [],
        }

        if len(samples) < 2:
            print("Insufficient samples for clustering (need >= 2)")
            with open("exports/sequence_clustering_summary.json", "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2)
            with open("exports/sequence_cluster_assignments.csv", "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "case_id",
                        "specimen_date",
                        "geographic_region",
                        "cluster_id",
                        "source",
                        "mean_comparable_sites_to_cluster",
                        "min_comparable_sites_to_cluster",
                        "max_comparable_sites_to_cluster",
                    ],
                )
                writer.writeheader()
            db.commit()
            print(json.dumps(summary, indent=2))
            print("=" * 70)
            print("DERIVE SEQUENCE CLUSTERS - COMPLETED (no clustering needed)")
            print("=" * 70)
            return

        uf = UnionFind([s["case_id"] for s in samples])
        sample_map = {s["case_id"]: s for s in samples}
        comparable_stats = {
            case_id: {"sum": 0, "count": 0, "min": None, "max": None}
            for case_id in sample_map.keys()
        }

        def _snp_distance_bin(dist: int) -> str:
            """Bin SNP distance into categorical ranges."""
            if dist <= 5:
                return "0-5"
            if dist <= 12:
                return "6-12"
            if dist <= 25:
                return "13-25"
            return ">25"

        pairwise_links = 0
        pairwise_comparable_sites: list[int] = []
        link_pair_comparable_sites: list[int] = []
        snp_distance_histogram: dict[str, int] = {}
        
        total_pairs = len(list(combinations(sample_map.keys(), 2)))
        print(f"\nComputing pairwise SNP distances ({total_pairs} pairs)...")
        processed = 0
        
        for a_id, b_id in combinations(sample_map.keys(), 2):
            processed += 1
            if processed % max(1, total_pairs // 10) == 0:
                print(f"  Progress: {processed}/{total_pairs} pairs ({100*processed//total_pairs}%)")
            
            dist, comparable_sites, _ambiguous_sites, _status = _snp_distance(
                sample_map[a_id]["sequence"], sample_map[b_id]["sequence"]
            )
            pairwise_comparable_sites.append(comparable_sites)
            key = _snp_distance_bin(dist)
            snp_distance_histogram[key] = snp_distance_histogram.get(key, 0) + 1
            if dist <= threshold:
                uf.union(a_id, b_id)
                pairwise_links += 1
                link_pair_comparable_sites.append(comparable_sites)
                for case_id in (a_id, b_id):
                    stats = comparable_stats[case_id]
                    stats["sum"] += comparable_sites
                    stats["count"] += 1
                    stats["min"] = comparable_sites if stats["min"] is None else min(stats["min"], comparable_sites)
                    stats["max"] = comparable_sites if stats["max"] is None else max(stats["max"], comparable_sites)

        groups = {}
        for case_id in sample_map.keys():
            root = uf.find(case_id)
            groups.setdefault(root, []).append(case_id)

        cluster_components = [sorted(members) for members in groups.values() if len(members) >= min_cluster_size]
        
        print(f"\nClustering results:")
        print(f"  Pairwise links under threshold: {pairwise_links}")
        print(f"  Components found: {len(groups)}")
        print(f"  Components >= min_cluster_size ({min_cluster_size}): {len(cluster_components)}")

        # Preserve investigation records across re-runs using a diff-based approach.
        # Instead of TRUNCATE (which destroys assignee, notes, actions, risk band overrides),
        # we load the existing cluster assignments, compare by membership, and only create
        # new cluster_investigations rows for genuinely new clusters. Unchanged clusters
        # (same UUID derived from same members) keep all their investigation history.

        # Load existing cluster memberships keyed by cluster_id
        print(f"\nLoading existing cluster memberships from database...")
        existing_cluster_rows = db.execute(
            text("SELECT cluster_id::text, sample_id::text FROM case_clusters")
        ).mappings().all()
        existing_clusters: dict[str, set[str]] = {}
        for r in existing_cluster_rows:
            existing_clusters.setdefault(r["cluster_id"], set()).add(r["sample_id"])
        print(f"  Found {len(existing_clusters)} existing clusters in database")

        # Compute new cluster UUIDs upfront (deterministic from members) so we can diff
        new_cluster_uuids: set[str] = set()
        for members in cluster_components:
            ns_key = "seqcluster:" + "|".join(sorted(members))
            new_cluster_uuids.add(str(uuid.uuid5(uuid.NAMESPACE_DNS, ns_key)))

        # Remove clusters that no longer exist and their case_clusters rows.
        # Preserve cluster_investigations for surviving clusters.
        stale_cluster_ids = set(existing_clusters.keys()) - new_cluster_uuids
        if stale_cluster_ids:
            print(f"  Removing {len(stale_cluster_ids)} stale clusters (no longer valid)...")
            for stale_id in stale_cluster_ids:
                db.execute(
                    text("DELETE FROM case_clusters WHERE cluster_id = CAST(:cid AS uuid)"),
                    {"cid": stale_id},
                )
                db.execute(
                    text("DELETE FROM clusters WHERE cluster_id = CAST(:cid AS uuid)"),
                    {"cid": stale_id},
                )

        # Remove all case_cluster assignments for surviving clusters so they can be re-inserted
        # (membership may have changed even if UUID is the same when members change).
        # The cluster and cluster_investigations rows are kept intact.
        for surviving_id in new_cluster_uuids.intersection(existing_clusters.keys()):
            db.execute(
                text("DELETE FROM case_clusters WHERE cluster_id = CAST(:cid AS uuid)"),
                {"cid": surviving_id},
            )

        assignments = []
        for idx, members in enumerate(sorted(cluster_components, key=len, reverse=True), start=1):
            namespace_key = "seqcluster:" + "|".join(sorted(members))
            cluster_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, namespace_key))
            is_new_cluster = cluster_uuid not in existing_clusters

            db.execute(
                text(
                    "INSERT INTO clusters (cluster_id, snp_distance, investigation_status, alert_flag) "
                    "VALUES (:cluster_id, :snp_distance, :investigation_status, :alert_flag) "
                    "ON CONFLICT (cluster_id) DO UPDATE SET "
                    "  alert_flag = EXCLUDED.alert_flag, "
                    "  snp_distance = EXCLUDED.snp_distance"
                ),
                {
                    "cluster_id": cluster_uuid,
                    "snp_distance": threshold,
                    "investigation_status": "open",
                    "alert_flag": len(members) >= alert_min_cluster_size,
                },
            )

            for case_id in members:
                db.execute(
                    text("INSERT INTO case_clusters (sample_id, cluster_id) VALUES (CAST(:sample_id AS uuid), CAST(:cluster_id AS uuid))"),
                    {"sample_id": case_id, "cluster_id": cluster_uuid},
                )
                s = sample_map[case_id]
                stats = comparable_stats.get(case_id, {"sum": 0, "count": 0, "min": None, "max": None})
                mean_sites = round(stats["sum"] / stats["count"], 2) if stats["count"] else None
                assignments.append(
                    {
                        "case_id": case_id,
                        "specimen_date": str(s.get("specimen_date") or ""),
                        "geographic_region": s.get("geographic_region") or "",
                        "cluster_id": cluster_uuid,
                        "source": "sequence_derived",
                        "mean_comparable_sites_to_cluster": mean_sites,
                        "min_comparable_sites_to_cluster": stats["min"],
                        "max_comparable_sites_to_cluster": stats["max"],
                    }
                )

            summary["clusters"].append(
                {
                    "cluster_label": f"SEQ-{idx:03d}",
                    "cluster_id": cluster_uuid,
                    "case_count": len(members),
                    "investigation_preserved": not is_new_cluster,
                }
            )

        summary["pairwise_links_under_threshold"] = pairwise_links
        if pairwise_comparable_sites:
            summary["pairwise_comparable_sites"] = {
                "mean": round(sum(pairwise_comparable_sites) / len(pairwise_comparable_sites), 2),
                "min": min(pairwise_comparable_sites),
                "max": max(pairwise_comparable_sites),
            }
        if link_pair_comparable_sites:
            summary["link_pair_comparable_sites"] = {
                "mean": round(sum(link_pair_comparable_sites) / len(link_pair_comparable_sites), 2),
                "min": min(link_pair_comparable_sites),
                "max": max(link_pair_comparable_sites),
            }
        summary["pairwise_snp_distance_histogram"] = {
            key: snp_distance_histogram[key]
            for key in sorted(
                snp_distance_histogram.keys(),
                key=lambda v: (999, v) if v == ">25" else (int(v.split("-")[0]), v)
            )
        }
        summary["cluster_count"] = len(summary["clusters"])
        summary["clustered_cases"] = sum(c["case_count"] for c in summary["clusters"])
        summary["unclustered_cases"] = len(samples) - summary["clustered_cases"]

        print(f"\nFinal summary:")
        print(f"  Total clusters: {summary['cluster_count']}")
        print(f"  Clustered cases: {summary['clustered_cases']}")
        print(f"  Unclustered cases: {summary['unclustered_cases']}")
        print(f"  Assignments to export: {len(assignments)}")

        with open("exports/sequence_clustering_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print("  Written: exports/sequence_clustering_summary.json")

        with open("exports/sequence_cluster_assignments.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "case_id",
                    "specimen_date",
                    "geographic_region",
                    "cluster_id",
                    "source",
                    "mean_comparable_sites_to_cluster",
                    "min_comparable_sites_to_cluster",
                    "max_comparable_sites_to_cluster",
                ],
            )
            writer.writeheader()
            for row in assignments:
                writer.writerow(row)
        print("  Written: exports/sequence_cluster_assignments.csv")

        print("\nWriting audit log...")
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
        print("Database committed successfully")
        
        print("\n" + "=" * 70)
        print("DERIVE SEQUENCE CLUSTERS - COMPLETED SUCCESSFULLY")
        print("=" * 70)
        print(json.dumps(summary, indent=2))
    except Exception as e:
        import traceback
        print("\n" + "=" * 70)
        print("DERIVE SEQUENCE CLUSTERS - FAILED")
        print("=" * 70)
        print(f"Error: {str(e)}")
        print("\nFull traceback:")
        traceback.print_exc()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()



