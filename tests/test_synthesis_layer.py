from backend.synthesis.scoring import cluster_priority_score
from backend.synthesis.transmission_synthesis import (
    _epi_support_level,
    _sequence_proxy_distance,
    build_transmission_synthesis,
)


def test_sequence_proxy_distance_uses_cluster_membership():
    assignments = {
        "case-a": {"cluster_id": "cluster-1"},
        "case-b": {"cluster_id": "cluster-1"},
        "case-c": {"cluster_id": "cluster-2"},
    }

    distance, source, same_cluster = _sequence_proxy_distance(
        "case-a",
        "case-b",
        assignments,
        threshold=25,
    )

    assert distance == 0
    assert source == "sequence_cluster_proxy"
    assert same_cluster is True


def test_sequence_proxy_distance_flags_cross_cluster_pairs():
    assignments = {
        "case-a": {"cluster_id": "cluster-1"},
        "case-c": {"cluster_id": "cluster-2"},
    }

    distance, source, same_cluster = _sequence_proxy_distance(
        "case-a",
        "case-c",
        assignments,
        threshold=25,
    )

    assert distance == 26
    assert source == "sequence_cluster_proxy"
    assert same_cluster is False


def test_cluster_priority_score_is_scaled_by_evidence():
    high_scale = cluster_priority_score(
        member_count=6,
        pair_count=10,
        strong_or_contradictory_pairs=3,
        cluster_flags=["cross_region_cluster"],
        evidence_scale=1.0,
    )
    low_scale = cluster_priority_score(
        member_count=6,
        pair_count=10,
        strong_or_contradictory_pairs=3,
        cluster_flags=["cross_region_cluster"],
        evidence_scale=0.25,
    )

    assert low_scale < high_scale


def test_epi_support_level_combines_temporal_and_geographic_context():
    assert _epi_support_level(True, True) == "temporal_and_geographic"
    assert _epi_support_level(True, False) == "temporal_only"
    assert _epi_support_level(False, True) == "geographic_only"
    assert _epi_support_level(False, False) == "none"


def test_transmission_synthesis_reports_validation_and_calibration(monkeypatch):
    from backend.synthesis import transmission_synthesis as mod

    monkeypatch.setattr(
        mod,
        "_case_rows",
        lambda db, cluster_id=None: [
            {
                "case_id": "case-a",
                "specimen_date": __import__("datetime").date(2026, 1, 1),
                "region": "A",
                "cluster_id": "cluster-1",
                "lineage": "L1",
                "predicted_drug_resistance": {},
                "sequence": "ACGT",
                "qc_status": "pass",
                "contamination_flag": False,
            },
            {
                "case_id": "case-b",
                "specimen_date": __import__("datetime").date(2026, 1, 10),
                "region": "A",
                "cluster_id": "cluster-1",
                "lineage": "L1",
                "predicted_drug_resistance": {},
                "sequence": "ACGT",
                "qc_status": "pass",
                "contamination_flag": False,
            },
        ],
    )
    monkeypatch.setattr(
        mod,
        "_load_sequence_proxy",
        lambda: (
            {
                "case-a": {"cluster_id": "cluster-1"},
                "case-b": {"cluster_id": "cluster-1"},
            },
            25,
            0.24736842105263157,
        ),
    )
    monkeypatch.setattr(
        mod,
        "_export_json",
        lambda path: {
            "edges": [
                {
                    "source": "case-a",
                    "target": "case-b",
                    "probability": 0.9,
                    "confidence": "high",
                }
            ]
        },
    )

    payload = build_transmission_synthesis(db=object(), cluster_id=None)

    assert payload["validation_status"] == "heuristic_non_validated"
    assert payload["calibration"]["sequence_cluster_threshold_snp_distance"] == 25
    assert payload["calibration"]["outbreaker_vs_sequence_pairwise_precision"] == 0.24736842105263157
    assert payload["pairs"][0]["snp_distance_source"] == "sequence_cluster_proxy"
    assert payload["pairs"][0]["sequence_cluster_match"] is True
    assert payload["pairs"][0]["epi_support"] == "temporal_and_geographic"
    assert payload["warning"].startswith("This synthesis output is heuristic")
