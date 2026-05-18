from __future__ import annotations

from backend.synthesis.scoring import category_label


_CATEGORY_TEXT = {
    "strong_support": "Genomic, model and temporal evidence support possible transmission.",
    "moderate_support": "Genomic evidence is present with partial model or timing support.",
    "genomic_only_signal": "Genomic proximity is present but model and epidemiological support are weak.",
    "model_only_signal": "Model posterior supports a link, but genomic distance is not supportive.",
    "contradictory": "Model and genomic evidence disagree and requires review.",
    "insufficient_evidence": "Insufficient sequence, timing, or model evidence for interpretation.",
}

_FLAG_ACTIONS = {
    "high_posterior_high_snp_distance": "Review sequence quality, contamination indicators, and metadata consistency.",
    "low_snp_no_known_epi_or_geographic_link": "Prioritise epidemiology review for travel, social links, and missing exposure history.",
    "same_cluster_wide_date_spread": "Check for cluster persistence, reactivation, or multiple transmission generations.",
    "resistance_signal_inside_cluster": "Escalate to AMR-focused review and verify resistance mutation concordance.",
    "cross_region_cluster": "Coordinate cross-region case review and confirm movement/exposure links.",
    "rapidly_growing_cluster": "Escalate contact tracing and increase cluster monitoring frequency.",
    "missing_sequence_or_qc_data": "Request missing sequencing/QC completion before final sign-off.",
    "low_confidence_outbreaker_edge": "Treat inferred direction cautiously and seek supporting evidence.",
    "lineage_discordance": "Verify lineage calls for both cases; discordant lineages are a strong counter-indication for direct transmission.",
    "resistance_profile_discordance": "Review drug resistance mutations for both cases; inconsistent profiles reduce transmission probability.",
    "resistance_profile_partial_overlap": "Resistance profiles partially overlap; consider whether resistance was acquired within the chain.",
    "temporally_implausible_directionality": "Source specimen postdates target — review dates and assess whether direction of transmission is reliable.",
    "low_sequence_coverage_for_pair": "One or both sequences have low depth or coverage; SNP-based evidence should be treated with caution.",
}


def interpretation_text(category: str, flags: list[str]) -> str:
    base = _CATEGORY_TEXT.get(category, _CATEGORY_TEXT["insufficient_evidence"])
    if not flags:
        return base
    if category == "contradictory":
        return base + " Contradiction flags indicate urgent manual reconciliation."
    return base


def recommended_actions(category: str, flags: list[str]) -> list[str]:
    actions: list[str] = []
    # Flags that are informational only and should not generate actions
    informational_flags = {"lineage_concordance", "resistance_profile_concordance"}

    if category in {"strong_support", "moderate_support"}:
        actions.append("Prioritise this pair for targeted contact tracing review.")
    if category == "contradictory":
        actions.append("Open contradiction review with genomics, epidemiology, and model outputs side-by-side.")
    if category == "insufficient_evidence":
        actions.append("Hold final interpretation until missing data are resolved.")

    for flag in flags:
        if flag in informational_flags:
            continue  # Skip informational flags
        action = _FLAG_ACTIONS.get(flag)
        if action and action not in actions:
            actions.append(action)

    if not actions:
        actions.append("Monitor in routine surveillance workflow.")

    return actions


def category_display(category: str) -> str:
    return category_label(category)

