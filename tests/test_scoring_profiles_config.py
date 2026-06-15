import json

import pytest

from backend.synthesis.scoring_profiles import ScoringProfileError, load_scoring_profile


def test_default_scoring_profile_loaded_from_config():
    profile = load_scoring_profile("default_v1")

    assert profile["version"] == "default_v1"
    assert profile["score_weights"]["strong epi support"] == 3
    assert profile["confidence_thresholds"] == {"strong": 5, "moderate": 3, "weak": 1}
    assert profile["missing_data_penalty_max"] == 2


def test_scoring_profile_loader_returns_copy():
    profile = load_scoring_profile("default_v1")
    profile["score_weights"]["strong epi support"] = 99

    fresh = load_scoring_profile("default_v1")

    assert fresh["score_weights"]["strong epi support"] == 3


def test_unknown_scoring_profile_rejected():
    with pytest.raises(ScoringProfileError):
        load_scoring_profile("does_not_exist")
