from backend.synthesis.scoring import cluster_priority_score
from backend.synthesis.transmission_synthesis import (
    _bool_temporal_support,
    _epi_support_level,
    _resistance_profile_concordance,
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


def test_bool_temporal_support_enforces_direction_with_tolerance():
    dt = __import__("datetime").date
    source = dt(2026, 1, 20)
    target = dt(2026, 1, 10)
    assert _bool_temporal_support(source, target, window_days=45, tolerance_days=5) is False
    assert _bool_temporal_support(source, target, window_days=45, tolerance_days=15) is True


def test_resistance_concordance_uses_mutation_identity_when_available():
    left_profile = {"rifampicin": "resistant"}
    right_profile = {"rifampicin": "resistant"}

    assert (
        _resistance_profile_concordance(
            left_profile,
            right_profile,
            [{"drug": "rifampicin", "gene": "rpoB", "mutation": "S450L"}],
            [{"drug": "rifampicin", "gene": "rpoB", "mutation": "H445Y"}],
        )
        == "discordant"
    )
    assert (
        _resistance_profile_concordance(
            left_profile,
            right_profile,
            [{"drug": "rifampicin", "gene": "rpoB", "mutation": "S450L"}],
            [{"drug": "rifampicin", "gene": "rpoB", "mutation": "S450L"}],
        )
        == "concordant"
    )


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
    # Return empty epi records so the proxy fallback is exercised.
    monkeypatch.setattr(
        mod,
        "load_epi_records_for_cases",
        lambda db, case_ids: {"contact_links": {}, "location_events": {}},
    )

    payload = build_transmission_synthesis(db=object(), cluster_id=None)

    assert payload["validation_status"] == "heuristic_non_validated"
    assert payload["calibration"]["sequence_cluster_threshold_snp_distance"] == 25
    assert payload["calibration"]["outbreaker_vs_sequence_pairwise_precision"] == 0.24736842105263157
    assert payload["pairs"][0]["snp_distance_source"] == "sequence_cluster_proxy"
    assert payload["pairs"][0]["sequence_cluster_match"] is True
    assert payload["pairs"][0]["epi_support"] == "temporal_and_geographic"
    assert "epi_evidence" in payload["pairs"][0]
    assert payload["warning"].startswith("This synthesis output is heuristic")


def test_transmission_synthesis_flags_lineage_and_resistance_discordance(monkeypatch):
    from backend.synthesis import transmission_synthesis as mod

    monkeypatch.setattr(
        mod,
        "_case_rows",
        lambda db, cluster_id=None: [
            {
                "case_id": "case-a",
                "specimen_date": __import__("datetime").date(2026, 2, 20),
                "region": "A",
                "cluster_id": "cluster-1",
                "lineage": "L2",
                "predicted_drug_resistance": {"rifampicin": "resistant"},
                "sequence": "ACGT",
                "mean_depth": 40.0,
                "coverage_breadth": 0.98,
                "qc_status": "pass",
                "contamination_flag": False,
            },
            {
                "case_id": "case-b",
                "specimen_date": __import__("datetime").date(2026, 1, 1),
                "region": "A",
                "cluster_id": "cluster-1",
                "lineage": "L4",
                "predicted_drug_resistance": {"isoniazid": "resistant"},
                "sequence": "ACGT",
                "mean_depth": 40.0,
                "coverage_breadth": 0.98,
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
            0.8,
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
                    "probability": 0.95,
                    "confidence": "high",
                }
            ]
        },
    )
    monkeypatch.setattr(
        mod,
        "load_epi_records_for_cases",
        lambda db, case_ids: {"contact_links": {}, "location_events": {}},
    )

    payload = build_transmission_synthesis(db=object(), cluster_id=None)
    pair = payload["pairs"][0]

    assert pair["lineage_concordance"] == "discordant"
    assert pair["resistance_profile_concordance"] == "discordant"
    assert "lineage_discordance" in pair["flags"]
    assert "resistance_profile_discordance" in pair["flags"]
    assert "temporally_implausible_direction" in pair["flags"]
    assert pair["confidence_code"] == "contradictory"


# ---------------------------------------------------------------------------
# epi_evidence module unit tests
# ---------------------------------------------------------------------------

def test_compute_epi_evidence_empty_records_returns_unknown_level():
    from backend.synthesis.epi_evidence import compute_epi_evidence

    result = compute_epi_evidence(
        "case-a",
        "case-b",
        {"contact_links": {}, "location_events": {}},
    )

    assert result["epi_support_level"] == "unknown"
    assert result["shared_contacts"] == []
    assert result["shared_locations"] == []
    assert result["shared_exposures"] == []
    assert "no_epi_records_case_a" in result["missing_data"]
    assert "no_epi_records_case_b" in result["missing_data"]


def test_compute_epi_evidence_shared_contact_raises_level():
    from datetime import date
    from backend.synthesis.epi_evidence import compute_epi_evidence

    shared_contact = {
        "link_id": "link-1",
        "contact_id": "contact-x",
        "contact_label": "HCW Facility A",
        "contact_type": "healthcare",
        "relationship_type": None,
        "exposure_id": None,
        "exposure_type": None,
        "confidence": "high",
        "exposure_start_date": None,
        "exposure_end_date": None,
    }
    records = {
        "contact_links": {
            "case-a": [shared_contact],
            "case-b": [shared_contact],
        },
        "location_events": {},
    }

    result = compute_epi_evidence(
        "case-a",
        "case-b",
        records,
        specimen_date_a=date(2026, 1, 1),
        specimen_date_b=date(2026, 1, 15),
        temporal_window_days=45,
    )

    assert result["epi_support_level"] in {"strong", "moderate"}
    assert len(result["shared_contacts"]) == 1
    assert result["shared_contacts"][0]["contact_id"] == "contact-x"
    assert result["temporal_overlap"] == "plausible"


def test_compute_epi_evidence_implausible_dates_with_shared_link_flags_contradiction():
    from datetime import date
    from backend.synthesis.epi_evidence import compute_epi_evidence

    shared_contact = {
        "link_id": "link-1",
        "contact_id": "contact-x",
        "contact_label": "HCW Facility A",
        "contact_type": "healthcare",
        "relationship_type": None,
        "exposure_id": None,
        "exposure_type": None,
        "confidence": "medium",
        "exposure_start_date": None,
        "exposure_end_date": None,
    }
    records = {
        "contact_links": {
            "case-a": [shared_contact],
            "case-b": [shared_contact],
        },
        "location_events": {},
    }

    result = compute_epi_evidence(
        "case-a",
        "case-b",
        records,
        specimen_date_a=date(2024, 1, 1),
        specimen_date_b=date(2026, 6, 1),  # > 2 years apart
        temporal_window_days=45,
    )

    assert result["temporal_overlap"] == "implausible"
    assert "shared_epi_link_but_implausible_temporal_overlap" in result["contradictions"]

