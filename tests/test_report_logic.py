from backend.routers.cases import _confidence_tier, _pct, _pct_label


def test_pct_uses_matching_denominator():
    assert _pct(16, 25) == 64.0
    assert _pct_label(_pct(7, 16)) == "43.8%"


def test_pct_handles_empty_denominator():
    assert _pct(0, 0) is None
    assert _pct_label(None) == "n/a"


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
