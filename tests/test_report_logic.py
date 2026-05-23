from backend.routers.cases import _confidence_tier, _pct, _pct_label
from backend.routers.outbreak_html_builder import _format_optional_float
from backend.routers.reports import render_actionable_surveillance_report_html
from scripts.generate_priority_visualizations import (
    _classification_matrix_value,
    _normalise_resistance_profile,
    _resistant_drug_count,
)


def test_pct_uses_matching_denominator():
    assert _pct(16, 25) == 64.0
    assert _pct_label(_pct(7, 16)) == "43.8%"


def test_pct_handles_empty_denominator():
    assert _pct(0, 0) is None
    assert _pct_label(None) == "n/a"


def test_report_optional_float_handles_null_artifact_values():
    assert _format_optional_float(None) == "n/a"
    assert _format_optional_float("not-a-number") == "n/a"
    assert _format_optional_float(0.12345) == "0.123"


def test_actionable_report_renders_advanced_fasta_summary():
    html = render_actionable_surveillance_report_html(
        {
            "generated_at": "2026-05-23T10:00:00Z",
            "status": "ready",
            "executive_summary": {},
            "surveillance_kpis": {},
            "data_readiness": {},
            "lineage_dr_summary": {},
            "advanced_fasta_summary": {
                "status": "completed",
                "input": {"sample_count": 10},
                "seqkit_stats": {"sum_len": "12000", "avg_len": "1200.0"},
                "snp_matrix_agreement": {"status": "pass", "compared_pairs": 36, "mismatch_count": 0},
                "iqtree": {"model": "GTR+F+G4"},
                "molecular_clock": {"r_squared": "0.00", "rate": "1.210e+00"},
                "artifact_counts": {"tbprofiler_json": 121},
                "warnings": ["TreeTime root-to-tip temporal signal is weak (r^2=0.00)."],
            },
            "warning": "Decision support only.",
        }
    )

    assert "Advanced FASTA Analysis" in html
    assert "SNP matrix check" in html
    assert "TreeTime r^2" in html
    assert "tbprofiler json" in html
    assert "TreeTime root-to-tip temporal signal is weak" in html


def test_resistance_heatmap_handles_overall_tbprofiler_classification():
    sensitive = {"classification": "Sensitive", "resistant_drugs": []}
    resistant = {"classification": "MDR", "resistant_drugs": ["rifampicin", "isoniazid"]}

    assert _classification_matrix_value(sensitive) == 1
    assert _classification_matrix_value(resistant) == 3
    assert _resistant_drug_count(resistant) == 2


def test_resistance_heatmap_expands_sensitive_summary_to_drug_columns():
    profile = _normalise_resistance_profile({"classification": "Sensitive", "resistant_drugs": []})

    assert profile["rifampicin"] == "susceptible"
    assert profile["isoniazid"] == "susceptible"
    assert profile["ethambutol"] == "susceptible"
    assert profile["pyrazinamide"] == "susceptible"


def test_resistance_heatmap_expands_resistant_drug_list_to_drug_columns():
    profile = _normalise_resistance_profile({"classification": "MDR-TB", "resistant_drugs": ["rifampicin", "INH"]})

    assert profile["rifampicin"] == "resistant"
    assert profile["isoniazid"] == "resistant"


def test_model_only_probability_stays_exploratory_without_snp_support():
    assert (
        _confidence_tier(
            qc_status="pass",
            contamination_flag=False,
            pairwise_distance=25,
            outbreaker_probability=0.95,
            same_cluster=True,
        )
        == "Exploratory"
    )


def test_snp_supported_pair_can_be_moderate_confidence():
    assert (
        _confidence_tier(
            qc_status="pass",
            contamination_flag=False,
            pairwise_distance=12,
            outbreaker_probability=0.20,
            same_cluster=True,
        )
        == "Moderate confidence"
    )

