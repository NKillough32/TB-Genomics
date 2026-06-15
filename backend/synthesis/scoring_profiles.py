"""Load configurable case-pair scoring profiles."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "scoring_profiles"
DEFAULT_SCORING_PROFILE = "default_v1"


class ScoringProfileError(ValueError):
    """Raised when a scoring profile is missing or invalid."""


def _profile_path(name: str) -> Path:
    key = str(name or DEFAULT_SCORING_PROFILE).strip().lower()
    if not key.replace("_", "").replace("-", "").isalnum():
        raise ScoringProfileError(f"Invalid scoring_profile '{name}'")
    return CONFIG_DIR / f"{key}.yaml"


def _load_yaml_subset(path: Path) -> dict[str, Any]:
    """Load the YAML profile file.

    Profiles are stored as JSON-compatible YAML so the application does not
    require an additional YAML parser at runtime. If PyYAML is installed, it can
    still read the same file; the standard-library JSON parser is sufficient.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise ScoringProfileError(f"Unknown scoring_profile '{path.stem}'") from exc
    except json.JSONDecodeError as exc:
        raise ScoringProfileError(f"Scoring profile '{path.name}' is not valid JSON-compatible YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise ScoringProfileError(f"Scoring profile '{path.name}' must contain an object")
    return payload


def _validate_profile(profile: dict[str, Any], expected_name: str) -> dict[str, Any]:
    required_sections = ("version", "score_weights", "confidence_thresholds", "missing_data_penalty_max")
    missing = [key for key in required_sections if key not in profile]
    if missing:
        raise ScoringProfileError(f"Scoring profile '{expected_name}' missing required keys: {', '.join(missing)}")
    if not isinstance(profile.get("score_weights"), dict):
        raise ScoringProfileError(f"Scoring profile '{expected_name}' score_weights must be an object")
    if not isinstance(profile.get("confidence_thresholds"), dict):
        raise ScoringProfileError(f"Scoring profile '{expected_name}' confidence_thresholds must be an object")
    try:
        int(profile.get("missing_data_penalty_max"))
    except (TypeError, ValueError) as exc:
        raise ScoringProfileError(f"Scoring profile '{expected_name}' missing_data_penalty_max must be an integer") from exc
    return profile


@lru_cache(maxsize=16)
def _cached_scoring_profile(name: str) -> str:
    path = _profile_path(name)
    profile = _validate_profile(_load_yaml_subset(path), name)
    return json.dumps(profile)


def load_scoring_profile(name: str | None = None) -> dict[str, Any]:
    """Return a validated scoring profile loaded from config/scoring_profiles."""
    key = str(name or DEFAULT_SCORING_PROFILE).strip().lower()
    return json.loads(_cached_scoring_profile(key))
