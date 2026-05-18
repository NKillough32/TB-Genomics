import json
from datetime import date

from backend.routers import analytics


def _parse_date(value):
    if value is None:
        return None
    return date.fromisoformat(value)


def _decode_pair_index(raw: dict) -> dict:
    decoded = {}
    for key, value in raw.items():
        left, right = key.split("|", 1)
        decoded[analytics._pair_key(left, right)] = value
    return decoded


def test_case_pair_benchmark_scenarios_produce_expected_interpretations():
    with open("tests/benchmarks/case_pair_scenarios.json", "r", encoding="utf-8") as handle:
        scenarios = json.load(handle)

    for scenario in scenarios:
        rows = []
        for row in scenario["rows"]:
            parsed = dict(row)
            parsed["specimen_date"] = _parse_date(row.get("specimen_date"))
            parsed.setdefault("qc_status", "pass")
            parsed.setdefault("contamination_flag", False)
            rows.append(parsed)

        payload = analytics._build_case_pair_evidence_payload(
            rows,
            _decode_pair_index(scenario.get("pair_epi_index") or {}),
            snp_strong_threshold=5,
            snp_moderate_threshold=12,
            temporal_window_days=45,
            max_pairs=20,
        )

        assert payload["pair_count"] >= 1, scenario["name"]
        pair = payload["pairs"][0]
        assert pair["overall_interpretation"] == scenario["expected"]["overall_interpretation"], scenario["name"]
        assert pair["evidence"]["basis"] == scenario["expected"]["basis"], scenario["name"]


def test_cluster_priority_reasons_include_recent_and_resistance_signals(monkeypatch):
    rows = [
        {
            "case_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "specimen_date": date.today(),
            "region": "Belfast",
            "predicted_drug_resistance": {"rifampicin": "resistant"},
        },
        {
            "case_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "specimen_date": date.today(),
            "region": "Belfast",
            "predicted_drug_resistance": {"rifampicin": "susceptible"},
        },
    ]

    monkeypatch.setattr(
        analytics,
        "_export_json",
        lambda _: {
            "edges": [
                {
                    "source": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "target": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    "probability": 0.91,
                }
            ]
        },
    )

    pair_epi_index = {
        analytics._pair_key(rows[0]["case_id"], rows[1]["case_id"]): {
            "domains": ["congregate_setting"],
            "shared_location_count": 1,
            "shared_contact_count": 0,
        }
    }

    payload = analytics._cluster_priority_reasons(
        rows,
        pair_epi_index,
        "11111111-1111-1111-1111-111111111111",
        recent_days=90,
        resistance_drug_keyword="rifamp",
    )

    assert payload["metrics"]["case_count"] == 2
    assert payload["metrics"]["recent_cases"] == 2
    assert payload["metrics"]["congregate_pairs"] == 1
    assert payload["metrics"]["resistant_cases"] == 1
    assert payload["metrics"]["high_confidence_edges"] == 1


def test_calibration_summary_reports_exact_and_binary_agreement():
    comparisons = [
        {"model_label": "probable transmission", "reviewer_label": "probable transmission"},
        {"model_label": "possible transmission", "reviewer_label": "confirmed transmission"},
        {"model_label": "unlikely transmission", "reviewer_label": "unlikely transmission"},
        {"model_label": "insufficient evidence", "reviewer_label": "possible transmission"},
    ]

    summary = analytics._calibration_summary(comparisons)

    assert summary["reviewed_pairs"] == 4
    assert summary["exact_agreement"] == 0.5
    assert summary["binary_agreement"] == 0.75
    assert summary["by_label"]["unlikely transmission"]["exact_agreement"] == 1.0


def test_case_pair_calibration_endpoint_compares_model_and_reviewer_labels(monkeypatch):
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
        },
        {
            "case_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "specimen_date": date(2026, 1, 4),
            "region": "Belfast",
            "cluster_id": "11111111-1111-1111-1111-111111111111",
            "lineage": "L4",
            "predicted_drug_resistance": {"rifampicin": "susceptible"},
            "qc_status": "pass",
            "contamination_flag": False,
            "sequence": "ACGTACGA",
        },
    ]

    monkeypatch.setattr(analytics, "_case_rows", lambda db: rows)
    monkeypatch.setattr(analytics, "_build_pair_epi_index", lambda db, case_ids: {})
    monkeypatch.setattr(
        analytics,
        "_latest_pair_review_map",
        lambda db, pair_keys: {
            analytics._pair_key(rows[0]["case_id"], rows[1]["case_id"]): {
                "classification": "possible transmission",
                "reviewer": "reviewer-a",
                "notes": None,
                "reviewed_at": "2026-05-01T00:00:00Z",
                "cluster_id": "11111111-1111-1111-1111-111111111111",
            }
        },
    )

    payload = analytics.case_pair_calibration(
        cluster_id=None,
        max_cases=10,
        max_pairs=10,
        snp_strong_threshold=5,
        snp_moderate_threshold=12,
        temporal_window_days=45,
        db=object(),
    )

    assert payload["model_pair_count"] == 1
    assert payload["reviewed_pair_count"] == 1
    assert payload["summary"]["reviewed_pairs"] == 1
    assert payload["comparisons"][0]["reviewer_label"] == "possible transmission"
    assert payload["comparisons"][0]["model_label"] in {
        "probable transmission",
        "possible transmission",
    }

