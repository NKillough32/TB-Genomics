from __future__ import annotations

from backend.synthesis.flags import (
    FLAG_CROSS_REGION,
    FLAG_HIGH_POSTERIOR_HIGH_SNP,
    FLAG_LINEAGE_DISCORDANCE,
    FLAG_LOW_CONF_OUTBREAKER_EDGE,
    FLAG_LOW_SEQUENCE_COVERAGE_FOR_PAIR,
    FLAG_LOW_SNP_NO_LINK,
    FLAG_MISSING_SEQUENCE_OR_QC,
    FLAG_RAPID_GROWTH,
    FLAG_RESISTANCE_IN_CLUSTER,
    FLAG_RESISTANCE_PROFILE_DISCORDANCE,
    FLAG_RESISTANCE_PROFILE_PARTIAL_OVERLAP,
    FLAG_TEMPORAL_IMPLAUSIBLE,
    FLAG_WIDE_DATE_SPREAD,
)
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
    FLAG_HIGH_POSTERIOR_HIGH_SNP: "Review sequence quality, contamination indicators, and metadata consistency.",
    FLAG_LOW_SNP_NO_LINK: "Prioritise epidemiology review for travel, social links, and missing exposure history.",
    FLAG_WIDE_DATE_SPREAD: "Check for cluster persistence, reactivation, or multiple transmission generations.",
    FLAG_RESISTANCE_IN_CLUSTER: "Escalate to AMR-focused review and verify resistance mutation concordance.",
    FLAG_CROSS_REGION: "Coordinate cross-region case review and confirm movement/exposure links.",
    FLAG_RAPID_GROWTH: "Escalate contact tracing and increase cluster monitoring frequency.",
    FLAG_MISSING_SEQUENCE_OR_QC: "Request missing sequencing/QC completion before final sign-off.",
    FLAG_LOW_CONF_OUTBREAKER_EDGE: "Treat inferred direction cautiously and seek supporting evidence.",
    FLAG_LINEAGE_DISCORDANCE: "Verify lineage calls for both cases; discordant lineages are a strong counter-indication for direct transmission.",
    FLAG_RESISTANCE_PROFILE_DISCORDANCE: "Review drug resistance mutations for both cases; inconsistent profiles reduce transmission probability.",
    FLAG_RESISTANCE_PROFILE_PARTIAL_OVERLAP: "Resistance profiles partially overlap; consider whether resistance was acquired within the chain.",
    FLAG_TEMPORAL_IMPLAUSIBLE: "Source specimen postdates target — review dates and assess whether direction of transmission is reliable.",
    FLAG_LOW_SEQUENCE_COVERAGE_FOR_PAIR: "One or both sequences have low depth or coverage; SNP-based evidence should be treated with caution.",
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

