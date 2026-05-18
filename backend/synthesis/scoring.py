from __future__ import annotations

from typing import Any


def _epi_supports_transmission(epi_support_level: str) -> bool:
    """Return True when the epi support level is considered corroborating."""
    return epi_support_level in {
        "strong",
        "moderate",
        "temporal_and_geographic",  # legacy proxy value
        "temporal_only",             # legacy proxy value
    }


def confidence_category(
    *,
    snp_distance: int | None,
    posterior_probability: float | None,
    epi_support_level: str = "unknown",
    low_snp_threshold: int,
    high_snp_contradiction_threshold: int,
    high_posterior_threshold: float,
) -> str:
    """Classify transmission support into operational synthesis categories.

    ``epi_support_level`` accepts both the structured values returned by
    :func:`~backend.synthesis.epi_evidence.compute_epi_evidence`
    (``'strong'``, ``'moderate'``, ``'weak'``, ``'temporal_only'``,
    ``'none'``, ``'unknown'``) and the older proxy values
    (``'temporal_and_geographic'``, ``'temporal_only'``, ``'geographic_only'``,
    ``'none'``) for backward compatibility.
    """
    if snp_distance is None or posterior_probability is None:
        return "insufficient_evidence"

    low_snp = snp_distance <= low_snp_threshold
    high_posterior = posterior_probability >= high_posterior_threshold
    has_epi = _epi_supports_transmission(epi_support_level)
    strong_epi = epi_support_level in {"strong", "temporal_and_geographic"}

    if high_posterior and snp_distance >= high_snp_contradiction_threshold:
        return "contradictory"
    if low_snp and high_posterior and strong_epi:
        return "strong_support"
    if low_snp and (high_posterior or has_epi):
        return "moderate_support"
    if low_snp:
        return "genomic_only_signal"
    if high_posterior:
        return "model_only_signal"
    return "insufficient_evidence"


def pair_priority_score(
    *,
    category: str,
    posterior_probability: float | None,
    snp_distance: int | None,
    pair_flags: list[str],
    epi_support_level: str | None = None,
) -> int:
    """Compute an operational priority score for a pair."""
    base_by_category = {
        "strong_support": 70,
        "moderate_support": 55,
        "genomic_only_signal": 45,
        "model_only_signal": 40,
        "contradictory": 65,
        "insufficient_evidence": 20,
    }
    score = base_by_category.get(category, 20)

    if posterior_probability is not None:
        score += int(round(min(1.0, max(0.0, posterior_probability)) * 20))

    if snp_distance is not None:
        if snp_distance <= 5:
            score += 8
        elif snp_distance <= 12:
            score += 4
        elif snp_distance >= 20:
            score -= 8

    if epi_support_level == "strong":
        score += 10
    elif epi_support_level in {"moderate", "temporal_and_geographic"}:
        score += 6
    elif epi_support_level in {"weak", "temporal_only", "geographic_only"}:
        score += 3

    score += min(15, len(pair_flags) * 4)
    return max(0, min(100, score))


def cluster_priority_score(
    *,
    member_count: int,
    pair_count: int,
    strong_or_contradictory_pairs: int,
    cluster_flags: list[str],
    evidence_scale: float = 1.0,
) -> int:
    """Compute a cluster-level investigation priority score."""
    score = 0
    score += min(35, member_count * 2)
    scaled_pairs = int(round(max(0.0, evidence_scale) * pair_count))
    scaled_strong_pairs = int(round(max(0.0, evidence_scale) * strong_or_contradictory_pairs))
    score += min(20, scaled_pairs)
    score += min(25, scaled_strong_pairs * 3)
    score += min(20, len(cluster_flags) * 4)
    return max(0, min(100, score))


def score_band(score: int) -> str:
    if score >= 80:
        return "critical"
    if score >= 60:
        return "high"
    if score >= 35:
        return "medium"
    return "low"


def category_label(category: str) -> str:
    labels: dict[str, str] = {
        "strong_support": "Strong support",
        "moderate_support": "Moderate support",
        "genomic_only_signal": "Genomic-only signal",
        "model_only_signal": "Model-only signal",
        "contradictory": "Contradictory",
        "insufficient_evidence": "Insufficient evidence",
    }
    return labels.get(category, category)


def serialise_parameters(values: dict[str, Any]) -> dict[str, Any]:
    """Keep config echoed in responses JSON-safe and explicit."""
    serialised = {
        "low_snp_threshold": int(values["low_snp_threshold"]),
        "high_snp_contradiction_threshold": int(values["high_snp_contradiction_threshold"]),
        "temporal_window_days": int(values["temporal_window_days"]),
        "high_posterior_threshold": float(values["high_posterior_threshold"]),
        "min_posterior": float(values["min_posterior"]),
        "rapid_growth_recent_days": int(values["rapid_growth_recent_days"]),
        "rapid_growth_case_threshold": int(values["rapid_growth_case_threshold"]),
        "wide_date_spread_days": int(values["wide_date_spread_days"]),
    }
    if "temporal_backfill_tolerance_days" in values:
        serialised["temporal_backfill_tolerance_days"] = int(values["temporal_backfill_tolerance_days"])
    return serialised

