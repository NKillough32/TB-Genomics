from datetime import date

from backend.routers import analytics


def test_case_pair_evidence_combines_genomic_and_epi_support(monkeypatch):
    rows = [
        {
            "case_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "specimen_date": date(2026, 1, 1),
            "region": "Belfast",
            "cluster_id": "cluster-1",
            "lineage": "L4",
            "predicted_drug_resistance": {"rifampicin": "susceptible"},
            "qc_status": "pass",
            "contamination_flag": False,
            "sequence": "ACGTACGT",
        },
        {
            "case_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "specimen_date": date(2026, 1, 5),
            "region": "Belfast",
            "cluster_id": "cluster-1",
            "lineage": "L4",
            "predicted_drug_resistance": {"rifampicin": "susceptible"},
            "qc_status": "pass",
            "contamination_flag": False,
            "sequence": "ACGTACGA",
        },
    ]

    monkeypatch.setattr(analytics, "_case_rows", lambda db: rows)
    monkeypatch.setattr(
        analytics,
        "_build_pair_epi_index",
        lambda db, case_ids: {
            analytics._pair_key(rows[0]["case_id"], rows[1]["case_id"]): {
                "domains": ["congregate_setting"],
                "supports": ["shared_locations:1"],
                "direct_observation": True,
                "shared_location_count": 1,
                "shared_contact_count": 0,
            }
        },
    )

    payload = analytics.case_pair_evidence(
        cluster_id=None,
        max_cases=10,
        max_pairs=10,
        snp_strong_threshold=5,
        snp_moderate_threshold=12,
        temporal_window_days=45,
        db=object(),
    )

    assert payload["pair_count"] == 1
    pair = payload["pairs"][0]
    assert pair["genomic_plausibility"]["support"] == "strong epi support"
    assert pair["epidemiological_support"]["support"] in {"moderate epi support", "strong epi support"}
    assert pair["evidence"]["basis"] == "genomic+epi"
    assert pair["evidence"]["observation_mode"] == "directly_observed"
    assert payload["scoring_profile_version"] == "default_v1"
    assert pair["evidence_card"]["confidence"] in {"strong", "moderate"}
    assert "data_completeness_score" in pair["evidence"]
    assert "directionality_hypothesis" in pair["evidence_card"]


def test_case_pair_evidence_surfaces_contradictions(monkeypatch):
    rows = [
        {
            "case_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "specimen_date": date(2026, 1, 1),
            "region": "A",
            "cluster_id": "cluster-2",
            "lineage": "L2",
            "predicted_drug_resistance": {"rifampicin": "resistant"},
            "qc_status": "pass",
            "contamination_flag": False,
            "sequence": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        },
        {
            "case_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "specimen_date": date(2026, 6, 1),
            "region": "B",
            "cluster_id": "cluster-2",
            "lineage": "L4",
            "predicted_drug_resistance": {"isoniazid": "resistant"},
            "qc_status": "pass",
            "contamination_flag": False,
            "sequence": "TTTTTTTTTTTTTTTTTTTTTTTTTTTTTT",
        },
    ]

    monkeypatch.setattr(analytics, "_case_rows", lambda db: rows)
    monkeypatch.setattr(analytics, "_build_pair_epi_index", lambda db, case_ids: {})

    payload = analytics.case_pair_evidence(
        cluster_id=None,
        max_cases=10,
        max_pairs=10,
        snp_strong_threshold=2,
        snp_moderate_threshold=4,
        temporal_window_days=45,
        db=object(),
    )

    pair = payload["pairs"][0]
    assert pair["overall_interpretation"] == "contradictory evidence"
    assert "lineage_mismatch" in pair["evidence"]["contradicts"]
    assert "resistance_profile_discordant" in pair["evidence"]["contradicts"]
    assert pair["genomic_plausibility"]["support"] == "contradictory"
    contradiction_codes = {item["code"] for item in pair["evidence"]["contradiction_details"]}
    assert "lineage_mismatch" in contradiction_codes


def test_case_pair_evidence_reports_unknown_when_data_missing(monkeypatch):
    rows = [
        {
            "case_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            "specimen_date": None,
            "region": "Unknown",
            "cluster_id": "",
            "lineage": "",
            "predicted_drug_resistance": {},
            "qc_status": "not_reported",
            "contamination_flag": False,
            "sequence": "",
        },
        {
            "case_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "specimen_date": None,
            "region": "Unknown",
            "cluster_id": "",
            "lineage": "",
            "predicted_drug_resistance": {},
            "qc_status": "not_reported",
            "contamination_flag": False,
            "sequence": "",
        },
    ]

    monkeypatch.setattr(analytics, "_case_rows", lambda db: rows)
    monkeypatch.setattr(analytics, "_build_pair_epi_index", lambda db, case_ids: {})

    payload = analytics.case_pair_evidence(
        cluster_id=None,
        max_cases=10,
        max_pairs=10,
        snp_strong_threshold=5,
        snp_moderate_threshold=12,
        temporal_window_days=45,
        db=object(),
    )

    pair = payload["pairs"][0]
    assert pair["evidence"]["basis"] == "insufficient"
    assert pair["overall_interpretation"] == "insufficient evidence"
    assert "missing_sequence" in pair["evidence"]["missing"]
    assert "missing_specimen_date" in pair["evidence"]["missing"]
    assert pair["evidence_card"]["data_completeness_score"] < 0.5


def test_pair_triage_queue_prioritises_unreviewed_high_confidence_pairs(monkeypatch):
    rows = [
        {
            "case_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "specimen_date": date(2026, 1, 1),
            "region": "Belfast",
            "cluster_id": "11111111-1111-1111-1111-111111111111",
            "lineage": "L4",
            "predicted_drug_resistance": {"rifampicin": "susceptible"},
            "qc_status": "pass",
            "contamination_flag": False,
            "sequence": "ACGTACGT",
            "symptom_onset_date": date(2025, 12, 20),
            "treatment_start_date": date(2026, 1, 10),
            "smear_status": "positive",
            "cavitation_status": "present",
            "culture_status": "positive",
            "culture_positivity_duration_days": 120,
        },
        {
            "case_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "specimen_date": date(2026, 1, 5),
            "region": "Belfast",
            "cluster_id": "11111111-1111-1111-1111-111111111111",
            "lineage": "L4",
            "predicted_drug_resistance": {"rifampicin": "susceptible"},
            "qc_status": "pass",
            "contamination_flag": False,
            "sequence": "ACGTACGA",
            "symptom_onset_date": date(2025, 12, 25),
            "treatment_start_date": date(2026, 1, 14),
            "smear_status": "positive",
            "cavitation_status": "present",
            "culture_status": "positive",
            "culture_positivity_duration_days": 95,
        },
    ]

    monkeypatch.setattr(analytics, "_case_rows", lambda db: rows)
    monkeypatch.setattr(
        analytics,
        "_build_pair_epi_index",
        lambda db, case_ids: {
            analytics._pair_key(rows[0]["case_id"], rows[1]["case_id"]): {
                "domains": ["congregate_setting"],
                "supports": ["shared_locations:1"],
                "direct_observation": True,
                "shared_location_count": 1,
                "shared_contact_count": 1,
            }
        },
    )
    monkeypatch.setattr(analytics, "_latest_pair_review_map", lambda db, pair_keys: {})

    payload = analytics.pair_triage_queue(
        cluster_id=None,
        max_cases=20,
        max_pairs=20,
        limit=10,
        scoring_profile="default_v1",
        snp_strong_threshold=5,
        snp_moderate_threshold=12,
        temporal_window_days=45,
        db=object(),
    )

    assert payload["queue_count"] == 1
    top = payload["queue"][0]
    assert top["review_status"] == "unreviewed"
    assert top["priority_score"] >= 3
    assert "unreviewed" in top["reasons"]
