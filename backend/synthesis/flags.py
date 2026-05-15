from __future__ import annotations

from datetime import date


FLAG_HIGH_POSTERIOR_HIGH_SNP = "high_posterior_high_snp_distance"
FLAG_LOW_SNP_NO_LINK = "low_snp_no_known_epi_or_geographic_link"
FLAG_LOW_CONF_OUTBREAKER_EDGE = "low_confidence_outbreaker_edge"
FLAG_MISSING_SEQUENCE_OR_QC = "missing_sequence_or_qc_data"

FLAG_WIDE_DATE_SPREAD = "same_cluster_wide_date_spread"
FLAG_RESISTANCE_IN_CLUSTER = "resistance_signal_inside_cluster"
FLAG_CROSS_REGION = "cross_region_cluster"
FLAG_RAPID_GROWTH = "rapidly_growing_cluster"


def pair_flags(
    *,
    snp_distance: int | None,
    posterior_probability: float | None,
    geographic_support: bool,
    edge_confidence: str,
    source_has_sequence: bool,
    target_has_sequence: bool,
    source_qc_ok: bool,
    target_qc_ok: bool,
    low_snp_threshold: int,
    high_snp_contradiction_threshold: int,
    high_posterior_threshold: float,
) -> list[str]:
    flags: list[str] = []

    if (
        snp_distance is not None
        and posterior_probability is not None
        and posterior_probability >= high_posterior_threshold
        and snp_distance >= high_snp_contradiction_threshold
    ):
        flags.append(FLAG_HIGH_POSTERIOR_HIGH_SNP)

    if snp_distance is not None and snp_distance <= low_snp_threshold and not geographic_support:
        flags.append(FLAG_LOW_SNP_NO_LINK)

    if (posterior_probability or 0.0) < high_posterior_threshold or edge_confidence.lower() == "low":
        flags.append(FLAG_LOW_CONF_OUTBREAKER_EDGE)

    if (not source_has_sequence) or (not target_has_sequence) or (not source_qc_ok) or (not target_qc_ok):
        flags.append(FLAG_MISSING_SEQUENCE_OR_QC)

    return flags


def cluster_flags(
    *,
    cluster_regions: set[str],
    specimen_dates: list[date],
    resistance_case_count: int,
    missing_sequence_or_qc_case_count: int,
    recent_case_count: int,
    wide_date_spread_days: int,
    rapid_growth_case_threshold: int,
) -> list[str]:
    flags: list[str] = []

    if len(cluster_regions) > 1:
        flags.append(FLAG_CROSS_REGION)

    if specimen_dates:
        span = (max(specimen_dates) - min(specimen_dates)).days
        if span >= wide_date_spread_days:
            flags.append(FLAG_WIDE_DATE_SPREAD)

    if resistance_case_count > 0:
        flags.append(FLAG_RESISTANCE_IN_CLUSTER)

    if recent_case_count >= rapid_growth_case_threshold:
        flags.append(FLAG_RAPID_GROWTH)

    if missing_sequence_or_qc_case_count > 0:
        flags.append(FLAG_MISSING_SEQUENCE_OR_QC)

    return flags
