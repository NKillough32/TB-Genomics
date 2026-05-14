from backend.routers.cases import _pct, _pct_label


def test_pct_uses_matching_denominator():
    assert _pct(16, 25) == 64.0
    assert _pct_label(_pct(7, 16)) == "43.8%"


def test_pct_handles_empty_denominator():
    assert _pct(0, 0) is None
    assert _pct_label(None) == "n/a"
