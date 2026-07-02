import json
from datetime import date
from pathlib import Path

from backend.routers import outbreak_report_data as report_data
from backend.routers.cases import _pair_key


class _FakeMappingResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeScalarResult:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def mappings(self):
        return _FakeMappingResult(self._rows)


class _FakeSession:
    def __init__(self):
        self.rollback_calls = 0

    def execute(self, statement):
        sql = " ".join(str(statement).split())

        if "SELECT COUNT(*) FROM cases" in sql:
            return _FakeScalarResult(scalar=3)
        if "SELECT COUNT(DISTINCT sample_id) FROM case_clusters" in sql:
            return _FakeScalarResult(scalar=3)
        if "SELECT COUNT(*) FROM clusters WHERE investigation_status = 'open'" in sql:
            return _FakeScalarResult(scalar=1)
        if "SELECT to_regclass('public.sample_qc_metrics') IS NOT NULL" in sql:
            return _FakeScalarResult(scalar=True)
        if "FROM weekly ORDER BY week_start" in sql:
            return _FakeScalarResult(
                rows=[
                    {
                        "week_start": date(2026, 6, 1),
                        "eligible_cases": 3,
                        "sequenced_cases": 2,
                        "sequenced_pct": 66.67,
                        "qc_pass_pct": 100.0,
                    }
                ]
            )
        if "FROM cases c LEFT JOIN case_clusters cc" in sql:
            return _FakeScalarResult(rows=_case_rows())
        if "FROM cluster_stats" in sql:
            return _FakeScalarResult(
                rows=[
                    {
                        "cluster_id": "cluster-1",
                        "case_count": 2,
                        "region_count": 1,
                        "most_recent_specimen": date(2026, 6, 5),
                        "recency_days": 20,
                        "investigation_status": "open",
                        "priority_score": 9,
                    },
                    {
                        "cluster_id": "cluster-2",
                        "case_count": 1,
                        "region_count": 1,
                        "most_recent_specimen": date(2026, 6, 10),
                        "recency_days": 15,
                        "investigation_status": "closed",
                        "priority_score": 5,
                    },
                ]
            )
        if "FROM base LEFT JOIN index_case" in sql:
            return _FakeScalarResult(
                rows=[
                    {
                        "cluster_id": "cluster-1",
                        "cases": 2,
                        "first_specimen": date(2026, 6, 1),
                        "latest_specimen": date(2026, 6, 5),
                        "median_snp_proxy": 3.0,
                        "max_snp_proxy": 3,
                        "rr_cases": 0,
                        "mdr_cases": 0,
                        "recent_30d": 2,
                        "recent_60d": 2,
                        "recent_90d": 2,
                        "investigation_status": "open",
                        "suspected_index_case": "case-a",
                    },
                    {
                        "cluster_id": "cluster-2",
                        "cases": 1,
                        "first_specimen": date(2026, 6, 10),
                        "latest_specimen": date(2026, 6, 10),
                        "median_snp_proxy": 15.0,
                        "max_snp_proxy": 15,
                        "rr_cases": 0,
                        "mdr_cases": 0,
                        "recent_30d": 1,
                        "recent_60d": 1,
                        "recent_90d": 1,
                        "investigation_status": "closed",
                        "suspected_index_case": "case-c",
                    },
                ]
            )

        raise AssertionError(f"Unexpected SQL: {sql}")

    def rollback(self):
        self.rollback_calls += 1


def _case_rows():
    return [
        {
            "case_id": "case-a",
            "specimen_date": date(2026, 6, 1),
            "geographic_region": "Belfast",
            "case_status": "open",
            "cluster_id": "cluster-1",
            "snp_distance": 3,
            "investigation_status": "open",
            "lineage": "L4.3",
            "predicted_drug_resistance": {"rifampicin": "susceptible"},
            "resistance_mutations": [],
            "interpretation_summary": "",
            "sequence": "ACGT",
            "qc_status": "pass",
            "coverage_breadth": 98.0,
            "mean_depth": 60.0,
            "contamination_flag": False,
            "ambiguous_base_percent": 0.2,
        },
        {
            "case_id": "case-b",
            "specimen_date": date(2026, 6, 5),
            "geographic_region": "Belfast",
            "case_status": "open",
            "cluster_id": "cluster-1",
            "snp_distance": 3,
            "investigation_status": "open",
            "lineage": "L4.3",
            "predicted_drug_resistance": {"rifampicin": "susceptible"},
            "resistance_mutations": [],
            "interpretation_summary": "",
            "sequence": "ACGA",
            "qc_status": "pass",
            "coverage_breadth": 97.0,
            "mean_depth": 55.0,
            "contamination_flag": False,
            "ambiguous_base_percent": 0.4,
        },
        {
            "case_id": "case-c",
            "specimen_date": date(2026, 6, 10),
            "geographic_region": "Derry",
            "case_status": "closed",
            "cluster_id": "cluster-2",
            "snp_distance": 15,
            "investigation_status": "closed",
            "lineage": "L2",
            "predicted_drug_resistance": {"isoniazid": "resistant"},
            "resistance_mutations": [],
            "interpretation_summary": "mixed lineage suspected",
            "sequence": "",
            "qc_status": "fail",
            "coverage_breadth": 80.0,
            "mean_depth": 12.0,
            "contamination_flag": True,
            "ambiguous_base_percent": 6.2,
        },
    ]


def _artifacts():
    return {
        "outbreaker_summary.json": {"data_provenance": "mock"},
        "transmission_network.json": {
            "high_confidence_edges": 2,
            "edges": [
                {
                    "source": "case-a",
                    "target": "case-b",
                    "probability": 0.91,
                    "chain_stability": {
                        "top_ancestor_agreement": 0.8,
                        "probability_min": 0.82,
                        "probability_max": 0.95,
                    },
                },
                {
                    "source": "case-a",
                    "target": "case-c",
                    "probability": 0.83,
                    "chain_stability": {
                        "top_ancestor_agreement": 0.42,
                        "probability_min": 0.5,
                        "probability_max": 0.83,
                    },
                },
            ],
        },
        "outbreaker_decycled_consensus.json": {"status": "ready"},
        "synthesis_output.json": {
            "format_version": "2026.06",
            "parameters": {
                "low_snp_threshold": 12,
                "high_snp_contradiction_threshold": 20,
                "high_posterior_threshold": 0.7,
            },
            "clusters": [
                {
                    "cluster_id": "cluster-1",
                    "lineage_distribution": {"L4": 2},
                    "summary": {
                        "transmission_generations": {
                            "max_generation": 2,
                            "sustained_transmission_flag": True,
                        }
                    },
                },
                {
                    "cluster_id": "cluster-2",
                    "lineage_distribution": {"L2": 1},
                    "summary": {
                        "transmission_generations": {
                            "max_generation": 0,
                            "sustained_transmission_flag": False,
                        }
                    },
                },
            ],
            "pairs": [
                {
                    "source": "case-a",
                    "target": "case-b",
                    "confidence": "Strong support",
                    "priority_score": 9,
                    "lineage_concordance": "concordant",
                    "resistance_profile_concordance": "concordant",
                    "flags": ["temporal_support"],
                },
                {
                    "source": "case-a",
                    "target": "case-c",
                    "confidence": "Contradictory",
                    "priority_score": 4,
                    "lineage_concordance": "discordant",
                    "resistance_profile_concordance": "discordant",
                    "flags": ["lineage_mismatch", "resistance_discordance"],
                },
            ],
        },
        "lineage_dr_validation.json": {"status": "ok"},
        "resistance_validation.json": {"status": "ok"},
        "secondary_engine_validation.json": {"status": "ok"},
        "cluster_method_comparison.json": {"precision_sequence_given_outbreaker": 0.8},
        "sequence_clustering_summary.json": {"cluster_count": 2},
        "fasta_analysis_summary.json": {"status": "completed"},
    }


def test_outbreak_report_model_snapshot(monkeypatch):
    monkeypatch.setattr(report_data, "_load_export_json", lambda name: _artifacts().get(name))
    monkeypatch.setattr(
        report_data,
        "surveillance_kpis",
        lambda weeks, db: {
            "eligible_cases": 3,
            "sequenced_cases": 2,
            "sequenced_pct": 66.67,
            "qc_pass_pct": 100.0,
        },
    )
    monkeypatch.setattr(
        report_data,
        "_pairwise_matrix",
        lambda sequence_by_case: {
            _pair_key("case-a", "case-b"): 3,
            _pair_key("case-a", "case-c"): 15,
        },
    )

    snapshot = report_data.outbreak_report_snapshot(
        report_data.build_outbreak_report_data(_FakeSession())
    )

    snapshot_path = Path(__file__).parent / "fixtures" / "outbreak_report_model_snapshot.json"
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot == expected
