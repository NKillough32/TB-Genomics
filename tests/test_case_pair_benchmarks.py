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
