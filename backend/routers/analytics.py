import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, StringConstraints
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.auth import AuthenticatedUser, require_roles
from backend.routers.dependencies import get_db
from backend.runtime_paths import EXPORTS_DIR
from backend.snp_validation import validated_snp_distance
from backend.synthesis.scoring_profiles import (
    DEFAULT_SCORING_PROFILE,
    ScoringProfileError,
    load_scoring_profile,
)
from backend.synthesis.transmission_synthesis import (
    SynthesisConfig,
    build_cluster_risk_summary,
    build_transmission_synthesis,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])
logger = logging.getLogger(__name__)

REVIEW_CLASSIFICATIONS = (
    "confirmed transmission",
    "probable transmission",
    "possible transmission",
    "unlikely transmission",
    "insufficient evidence",
)

ReviewClassification = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern="^(confirmed transmission|probable transmission|possible transmission|unlikely transmission|insufficient evidence)$",
    ),
]


def _resolve_scoring_profile(name: str | None) -> dict:
    try:
        return load_scoring_profile(name or DEFAULT_SCORING_PROFILE)
    except ScoringProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _contradiction_details(contradictions: list[str]) -> list[dict]:
    details = []
    for code in sorted(set(contradictions)):
        if code.startswith("high_snp_distance"):
            details.append({"code": code, "severity": "major", "rationale": "SNP distance exceeds plausible linkage threshold"})
        elif code == "lineage_mismatch":
            details.append({"code": code, "severity": "major", "rationale": "Cases have discordant lineage assignment"})
        elif code == "resistance_profile_discordant":
            details.append({"code": code, "severity": "major", "rationale": "Resistance profiles are discordant"})
        elif code.startswith("temporal_gap_exceeds"):
            details.append({"code": code, "severity": "minor", "rationale": "Specimen timing exceeds configured plausibility window"})
        elif code == "infectious_period_non_overlap":
            details.append({"code": code, "severity": "major", "rationale": "Estimated infectious periods do not overlap"})
        else:
            details.append({"code": code, "severity": "minor", "rationale": "Potential inconsistency detected"})
    return details


def _pair_data_completeness_score(left: dict, right: dict) -> float:
    # Core evidence completeness used for scoring penalty.
    core_checks = [
        bool(left.get("sequence")) and bool(right.get("sequence")),
        bool(left.get("lineage")) and bool(right.get("lineage")),
        bool(left.get("specimen_date")) and bool(right.get("specimen_date")),
        bool(left.get("predicted_drug_resistance")) and bool(right.get("predicted_drug_resistance")),
    ]
    core_available = sum(1 for present in core_checks if present)
    return round(core_available / len(core_checks), 3)


class CasePairReviewUpsert(BaseModel):
    case_a: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=36)]
    case_b: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=36)]
    reviewer_classification: ReviewClassification
    notes: str | None = None
    reviewer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None
    cluster_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=36)] | None = None


class CasePairReviewEnteredInError(BaseModel):
    case_a: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=36)]
    case_b: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=36)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    reviewer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None


class CasePairReviewRestore(BaseModel):
    case_a: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=36)]
    case_b: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=36)]
    reviewer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None


class AlertAssignment(BaseModel):
    assigned_to: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AlertStatusUpdate(BaseModel):
    note: str | None = None


class ActionCreate(BaseModel):
    alert_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=36)] | None = None
    cluster_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=36)] | None = None
    sample_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=36)] | None = None
    action_type: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    owner: str | None = None
    note: str | None = None
    due_at: datetime | None = None


class ActionUpdate(BaseModel):
    status: str | None = None
    owner: str | None = None
    note: str | None = None
    completed_by: str | None = None


def _json_ready(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _audit(db: Session, *, action: str, user_id: str, details: dict) -> None:
    db.execute(
        text(
            """
            INSERT INTO audit_log (action, user_id, details, timestamp)
            VALUES (:action, :user_id, CAST(:details AS JSONB), NOW())
            """
        ),
        {"action": action, "user_id": user_id, "details": json.dumps(details, default=str)},
    )


def _export_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def _read_export_text(path: str) -> str:
    try:
        full_path = Path(EXPORTS_DIR) / path
        if not full_path.exists() or not full_path.is_file():
            return ""
        return full_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _parse_tsv(text_value: str) -> list[dict[str, str]]:
    lines = [line for line in text_value.splitlines() if line.strip()]
    if not lines:
        return []
    headers = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        values = line.split("\t")
        rows.append({header: values[idx] if idx < len(values) else "" for idx, header in enumerate(headers)})
    return rows


def _parse_molecular_clock(text_value: str) -> dict:
    result = {}
    for line in text_value.splitlines():
        stripped = line.strip()
        if stripped.startswith("--rate:"):
            result["rate"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("--r^2:"):
            raw = stripped.split(":", 1)[1].strip()
            result["r_squared"] = raw
            try:
                result["r_squared_value"] = float(raw)
            except ValueError:
                logger.warning("Failed to parse r_squared value %r in plink summary", raw, exc_info=True)
    return result


def _parse_iqtree_summary(text_value: str) -> dict:
    result = {"warnings": []}
    for line in text_value.splitlines():
        stripped = line.strip()
        if stripped.startswith("Input data:"):
            result["input_data"] = stripped.replace("Input data:", "").strip()
        elif stripped.startswith("Number of parsimony informative sites:"):
            result["parsimony_informative_sites"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Model of substitution:"):
            result["model"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Log-likelihood of the tree:"):
            result["log_likelihood"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Total tree length"):
            result["total_tree_length"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("WARNING:"):
            result["warnings"].append(stripped)
    return result


def _external_snp_matrix() -> dict[str, dict[str, int]]:
    rows = _parse_tsv(_read_export_text("fasta_analysis/snp_distance_matrix.tsv"))
    if not rows:
        return {}
    matrix = {}
    for row in rows:
        case_id = row.get("") or row.get("ID") or row.get("id") or row.get("sample") or next(iter(row.values()), "")
        if not case_id:
            continue
        matrix[case_id] = {}
        for key, value in row.items():
            if not key or key == case_id:
                continue
            try:
                matrix[case_id][key] = int(float(value))
            except (TypeError, ValueError):
                continue
    return matrix


def _snp_matrix_agreement(db: Session) -> dict:
    external = _external_snp_matrix()
    if not external:
        return {"status": "not_available", "message": "snp-dists matrix artifact not found."}

    rows = db.execute(
        text(
            """
            SELECT sample_id::text AS case_id, sequence
            FROM consensus_sequences
            WHERE sequence IS NOT NULL
            """
        )
    ).mappings().all()
    sequence_by_case = {str(row["case_id"]): str(row["sequence"]) for row in rows}

    compared = 0
    mismatches = []
    max_abs_delta = 0
    for left, right_values in external.items():
        if left not in sequence_by_case:
            continue
        for right, external_distance in right_values.items():
            if right not in sequence_by_case or left >= right:
                continue
            internal_distance = validated_snp_distance(sequence_by_case[left], sequence_by_case[right]).distance
            delta = internal_distance - external_distance
            compared += 1
            max_abs_delta = max(max_abs_delta, abs(delta))
            if delta != 0:
                mismatches.append(
                    {
                        "case_a": left,
                        "case_b": right,
                        "case_a_short": left[:8],
                        "case_b_short": right[:8],
                        "internal_distance": internal_distance,
                        "external_distance": external_distance,
                        "delta": delta,
                    }
                )

    return {
        "status": "pass" if not mismatches and compared else ("review" if mismatches else "not_comparable"),
        "compared_pairs": compared,
        "mismatch_count": len(mismatches),
        "max_abs_delta": max_abs_delta,
        "mismatches": mismatches[:25],
    }


def _float_or_default(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _load_transmission_edges() -> list[dict]:
    """Load transmission edges from available export artifacts.

    Supports legacy `transmission_network.json` edges and current
    `synthesis_output.json` pairwise_transmission_evidence payloads.
    """
    net = _export_json(str(EXPORTS_DIR / "transmission_network.json")) or {}
    edges = net.get("edges") or []
    normalised = []
    for edge in edges:
        src = str(edge.get("source") or "")
        tgt = str(edge.get("target") or "")
        if not src or not tgt:
            continue
        normalised.append({
            "source": src,
            "target": tgt,
            "posterior": _float_or_default(edge.get("probability"), 0.0),
            "confidence": str(edge.get("confidence") or "unknown"),
            "posterior_reliability": str(edge.get("posterior_reliability") or net.get("posterior_reliability_status") or "unknown"),
            "posterior_reliability_reasons": net.get("posterior_reliability_reasons") or [],
        })
    if normalised:
        return normalised

    synthesis = _export_json(str(EXPORTS_DIR / "synthesis_output.json")) or {}
    fallback_edges: dict[tuple[str, str], dict] = {}
    for cluster in synthesis.get("clusters") or []:
        for pair in cluster.get("pairwise_transmission_evidence") or []:
            src = str(pair.get("source") or "")
            tgt = str(pair.get("target") or "")
            if not src or not tgt:
                continue
            key = (src, tgt)
            candidate = {
                "source": src,
                "target": tgt,
                "posterior": _float_or_default(pair.get("posterior_probability"), 0.0),
                "confidence": str(pair.get("confidence") or pair.get("confidence_code") or "unknown"),
                "posterior_reliability": "unknown",
                "posterior_reliability_reasons": [],
            }
            existing = fallback_edges.get(key)
            if not existing or candidate["posterior"] > existing["posterior"]:
                fallback_edges[key] = candidate

    return list(fallback_edges.values())


def _normalise_uuid(value: str) -> str:
    # Keep this light without importing uuid for every endpoint.
    val = (value or "").strip()
    if len(val) != 36 or val.count("-") != 4:
        raise HTTPException(status_code=422, detail="cluster_id must be a UUID")
    return val


def _snp_distance(a: str, b: str) -> int:
    return validated_snp_distance(a, b).distance


def _case_rows(db: Session):
    return db.execute(text("""
        SELECT c.pseudonymised_case_id::text AS case_id,
               c.specimen_date,
               COALESCE(c.geographic_region, 'Unknown') AS region,
               COALESCE(cc.cluster_id::text, '') AS cluster_id,
               COALESCE(ti.lineage, '') AS lineage,
               ti.predicted_drug_resistance,
               LOWER(COALESCE(sqm.qc_status, 'not_reported')) AS qc_status,
               COALESCE(sqm.contamination_flag, FALSE) AS contamination_flag,
               cs.sequence,
               c.symptom_onset_date,
               c.treatment_start_date,
               COALESCE(c.smear_status, 'unknown') AS smear_status,
               COALESCE(c.cavitation_status, 'unknown') AS cavitation_status,
               COALESCE(c.culture_status, 'unknown') AS culture_status,
               c.culture_positivity_duration_days,
               c.infectiousness_notes
        FROM cases c
        LEFT JOIN case_clusters cc ON cc.sample_id = c.pseudonymised_case_id
        LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
        LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id = c.pseudonymised_case_id
        LEFT JOIN consensus_sequences cs ON cs.sample_id = c.pseudonymised_case_id
    """)).mappings().all()


def _validation_notice() -> dict[str, str]:
    return {
        "validation_status": "heuristic_non_validated",
        "warning": (
            "This analytics output is heuristic and non-validated. Scores and interpretations "
            "require calibrated pipelines before real-world use."
        ),
    }


def _latest_pair_review_map(db: Session, pair_keys: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    if not pair_keys:
        return {}

    try:
        rows = db.execute(text("""
            SELECT case_a::text AS case_a,
                   case_b::text AS case_b,
                   reviewer_classification,
                   reviewer,
                   notes,
                   source_cluster_id::text AS source_cluster_id,
                   reviewed_at
            FROM case_pair_reviews
            WHERE COALESCE(entered_in_error, FALSE) = FALSE
            ORDER BY reviewed_at DESC
            LIMIT 5000
        """)).mappings().all()
    except Exception:
        return {}

    review_map: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = _pair_key(str(row.get("case_a") or ""), str(row.get("case_b") or ""))
        if key in pair_keys and key not in review_map:
            review_map[key] = {
                "classification": str(row.get("reviewer_classification") or ""),
                "reviewer": str(row.get("reviewer") or ""),
                "notes": row.get("notes"),
                "reviewed_at": row.get("reviewed_at").isoformat() if row.get("reviewed_at") else None,
                "cluster_id": str(row.get("source_cluster_id") or "") or None,
            }
    return review_map


def _cluster_priority_reasons(
    rows: list,
    pair_epi_index: dict[tuple[str, str], dict],
    cluster_id: str,
    *,
    recent_days: int,
    resistance_drug_keyword: str,
) -> dict:
    now = datetime.utcnow().date()
    case_count = len(rows)
    recent_cases = 0
    regions: set[str] = set()
    resistant_cases = 0

    for row in rows:
        specimen = row.get("specimen_date")
        if specimen and (now - specimen).days <= recent_days:
            recent_cases += 1
        region = str(row.get("region") or "Unknown")
        if region and region != "Unknown":
            regions.add(region)
        resistant = _resistant_drug_set(row.get("predicted_drug_resistance"))
        if any(resistance_drug_keyword.lower() in drug for drug in resistant):
            resistant_cases += 1

    case_ids = sorted(str(r.get("case_id")) for r in rows if r.get("case_id"))
    congregate_pairs = 0
    healthcare_pairs = 0
    household_pairs = 0
    for case_a, case_b in combinations(case_ids, 2):
        domains = set((pair_epi_index.get(_pair_key(case_a, case_b), {}) or {}).get("domains") or [])
        if "congregate_setting" in domains:
            congregate_pairs += 1
        if "healthcare_exposure" in domains:
            healthcare_pairs += 1
        if "household" in domains:
            household_pairs += 1

    edges = _load_transmission_edges()
    cluster_set = set(case_ids)
    high_conf_edges = 0
    for edge in edges:
        src = str(edge.get("source") or "")
        tgt = str(edge.get("target") or "")
        if src in cluster_set and tgt in cluster_set and _float_or_default(edge.get("posterior"), 0.0) >= 0.70:
            high_conf_edges += 1

    reasons: list[str] = []
    reasons.append(f"{case_count} cases are assigned to this genomic cluster")
    if recent_cases:
        reasons.append(f"{recent_cases} cases have specimen dates within the last {recent_days} days")
    if congregate_pairs:
        reasons.append(f"{congregate_pairs} case-pairs have shared congregate exposure evidence")
    if household_pairs:
        reasons.append(f"{household_pairs} case-pairs have household linkage evidence")
    if healthcare_pairs:
        reasons.append(f"{healthcare_pairs} case-pairs have shared healthcare exposure evidence")
    if resistant_cases:
        reasons.append(f"{resistant_cases} cases include {resistance_drug_keyword} resistance signals")
    if len(regions) > 1:
        reasons.append(f"geography spans {len(regions)} regions ({', '.join(sorted(regions))})")
    if high_conf_edges:
        reasons.append(f"{high_conf_edges} high-confidence transmission edges are present in model outputs")

    if not reasons:
        reasons = ["No strong prioritisation signals were detected from currently available evidence"]

    return {
        "cluster_id": cluster_id,
        "reasons": reasons,
        "metrics": {
            "case_count": case_count,
            "recent_cases": recent_cases,
            "congregate_pairs": congregate_pairs,
            "household_pairs": household_pairs,
            "healthcare_pairs": healthcare_pairs,
            "resistant_cases": resistant_cases,
            "region_count": len(regions),
            "high_confidence_edges": high_conf_edges,
        },
    }


def _pair_key(case_a: str, case_b: str) -> tuple[str, str]:
    return tuple(sorted([str(case_a), str(case_b)]))


def _infer_epi_domain(location_type: str, exposure_type: str, context: str) -> str:
    blob = f"{location_type} {exposure_type} {context}".lower()
    if any(token in blob for token in ("household", "home", "address", "visitor")):
        return "household"
    if any(token in blob for token in ("prison", "hostel", "shelter", "work", "workplace", "school")):
        return "congregate_setting"
    if any(token in blob for token in ("ward", "clinic", "hospital", "health", "healthcare", "waiting room")):
        return "healthcare_exposure"
    if any(token in blob for token in ("social", "event", "venue", "community", "group")):
        return "social_exposure"
    if any(token in blob for token in ("travel", "migration", "arrival", "route", "country")):
        return "travel_migration"
    return "unknown"


def _resistant_drug_set(predicted_dr: object) -> set[str]:
    resistant: set[str] = set()
    if isinstance(predicted_dr, dict):
        for drug, value in predicted_dr.items():
            status = str(value).strip().lower()
            if status in {"r", "resistant"} or "resistant" in status:
                resistant.add(str(drug).strip().lower())
    return resistant


# --- Infectiousness helpers ----------------------------------------------------

def _compute_infectious_period(row: dict) -> dict:
    """Estimate the likely infectious window for a TB case.

    Uses clinical fields when available; falls back to a specimen-date proxy.
    TB is typically infectious from ~4 weeks before symptom onset and becomes
    non-infectious ~2-3 weeks after effective treatment starts.
    """
    symptom_onset = row.get("symptom_onset_date")
    treatment_start = row.get("treatment_start_date")
    specimen = row.get("specimen_date")
    smear = str(row.get("smear_status") or "unknown").lower()
    cavitation = str(row.get("cavitation_status") or "unknown").lower()

    if symptom_onset and treatment_start:
        inf_start = symptom_onset - timedelta(days=28)
        # Smear-positive or cavitating disease: longer infectious tail
        tail_days = 21 if (smear == "positive" or cavitation == "present") else 14
        inf_end = treatment_start + timedelta(days=tail_days)
        return {"start": inf_start, "end": inf_end, "basis": "clinical_data"}

    if symptom_onset:
        inf_start = symptom_onset - timedelta(days=28)
        inf_end = (specimen or symptom_onset) + timedelta(days=90)
        return {"start": inf_start, "end": inf_end, "basis": "symptom_onset_only"}

    if specimen:
        inf_start = specimen - timedelta(days=60)
        inf_end = specimen + timedelta(days=90)
        return {"start": inf_start, "end": inf_end, "basis": "specimen_date_proxy"}

    return {"start": None, "end": None, "basis": "unavailable"}


def _infectiousness_weight(row: dict) -> int:
    """Return an integer infectiousness modifier (-2 to +3) based on clinical fields."""
    smear = str(row.get("smear_status") or "unknown").lower()
    cavitation = str(row.get("cavitation_status") or "unknown").lower()
    culture = str(row.get("culture_status") or "unknown").lower()
    culture_days = row.get("culture_positivity_duration_days")

    weight = 0
    if smear == "positive":
        weight += 1
    elif smear == "negative":
        weight -= 1
    if cavitation == "present":
        weight += 1
    if culture == "positive":
        weight += 1
    if culture_days and int(culture_days) > 90:
        weight += 1
    return max(-2, min(3, weight))


def _infectiousness_description(row: dict) -> str:
    """Human-readable summary of infectiousness indicators."""
    parts = []
    smear = str(row.get("smear_status") or "unknown").lower()
    cavitation = str(row.get("cavitation_status") or "unknown").lower()
    culture = str(row.get("culture_status") or "unknown").lower()
    culture_days = row.get("culture_positivity_duration_days")

    if smear in ("positive", "negative"):
        parts.append(f"smear-{smear}")
    if cavitation in ("present", "absent"):
        parts.append(f"cavitation {cavitation}")
    if culture in ("positive", "negative"):
        parts.append(f"culture-{culture}")
    if culture_days:
        parts.append(f"culture positivity {culture_days}d")
    return ", ".join(parts) if parts else "no infectiousness data"


def _infectious_period_overlap(row_a: dict, row_b: dict) -> dict:
    """Check whether two cases' estimated infectious periods overlap."""
    period_a = _compute_infectious_period(row_a)
    period_b = _compute_infectious_period(row_b)

    if not period_a["start"] or not period_b["start"]:
        return {
            "available": False,
            "overlap": None,
            "overlap_days": None,
            "basis_a": period_a["basis"],
            "basis_b": period_b["basis"],
        }

    overlap_start = max(period_a["start"], period_b["start"])
    overlap_end = min(period_a["end"], period_b["end"])

    if overlap_end >= overlap_start:
        overlap_days = (overlap_end - overlap_start).days
        return {
            "available": True,
            "overlap": True,
            "overlap_days": overlap_days,
            "basis_a": period_a["basis"],
            "basis_b": period_b["basis"],
        }
    return {
        "available": True,
        "overlap": False,
        "overlap_days": 0,
        "basis_a": period_a["basis"],
        "basis_b": period_b["basis"],
    }


def _directionality_hypothesis(row_a: dict, row_b: dict, overlap: dict) -> dict:
    specimen_a = row_a.get("specimen_date")
    specimen_b = row_b.get("specimen_date")
    case_a = str(row_a.get("case_id") or "")
    case_b = str(row_b.get("case_id") or "")

    if not specimen_a or not specimen_b or not overlap.get("available"):
        return {
            "likely_source": None,
            "likely_recipient": None,
            "confidence": "low",
            "rationale": "Insufficient temporal or infectious-period data for directionality",
        }

    if specimen_a == specimen_b:
        return {
            "likely_source": None,
            "likely_recipient": None,
            "confidence": "low",
            "rationale": "Specimen dates are identical; cannot infer direction",
        }

    source, recipient = (case_a, case_b) if specimen_a < specimen_b else (case_b, case_a)
    days_between = abs((specimen_b - specimen_a).days)
    confidence = "moderate" if overlap.get("overlap") and days_between <= 45 else "low"

    return {
        "likely_source": source,
        "likely_recipient": recipient,
        "confidence": confidence,
        "rationale": (
            "Earlier specimen date aligns with estimated infectious period overlap"
            if confidence == "moderate"
            else "Temporal order suggests direction but overlap support is limited"
        ),
    }


def _build_evidence_card(
    *,
    snp_distance: int | None,
    snp_strong_threshold: int,
    snp_moderate_threshold: int,
    lineage_a: str,
    lineage_b: str,
    same_lineage: bool,
    resistance_concordance: str,
    same_region: bool,
    region_a: str,
    region_b: str,
    temporal_delta_days: int | None,
    temporal_plausible: bool | None,
    temporal_window_days: int,
    epi_domains: list,
    epi_shared_locations: int,
    epi_shared_contacts: int,
    row_a: dict,
    row_b: dict,
    overall_score: int,
    contradictory_evidence: list,
    contradiction_details: list[dict],
    data_completeness_score: float,
    confidence_thresholds: dict,
) -> dict:
    """Build a structured transmission evidence card for a single case pair.

    Returns a signal table (one row per evidence dimension), a confidence label,
    and a plain-language interpretation paragraph.
    """
    signals: list[dict] = []

    # -- Genomic: SNP distance --------------------------------------------------
    if snp_distance is None:
        signals.append({"signal": "SNP distance", "value": "No sequence available", "direction": "missing"})
    elif snp_distance <= snp_strong_threshold:
        signals.append({"signal": "SNP distance", "value": f"{snp_distance} SNPs (<={snp_strong_threshold} - strong linkage)", "direction": "supports"})
    elif snp_distance <= snp_moderate_threshold:
        signals.append({"signal": "SNP distance", "value": f"{snp_distance} SNPs (moderate range <={snp_moderate_threshold})", "direction": "supports"})
    elif snp_distance <= 20:
        signals.append({"signal": "SNP distance", "value": f"{snp_distance} SNPs (elevated - weak linkage)", "direction": "weak"})
    else:
        signals.append({"signal": "SNP distance", "value": f"{snp_distance} SNPs (exceeds linkage threshold)", "direction": "contradicts"})

    # -- Genomic: Lineage -------------------------------------------------------
    if lineage_a and lineage_b:
        if same_lineage:
            signals.append({"signal": "Lineage", "value": f"Matched ({lineage_a})", "direction": "supports"})
        else:
            signals.append({"signal": "Lineage", "value": f"Mismatched ({lineage_a} vs {lineage_b})", "direction": "contradicts"})
    else:
        signals.append({"signal": "Lineage", "value": "Lineage data incomplete", "direction": "missing"})

    # -- Genomic: Resistance profile --------------------------------------------
    if resistance_concordance == "concordant":
        signals.append({"signal": "Resistance profile", "value": "Concordant", "direction": "supports"})
    elif resistance_concordance == "partially_concordant":
        signals.append({"signal": "Resistance profile", "value": "Partial overlap", "direction": "neutral"})
    elif resistance_concordance == "discordant":
        signals.append({"signal": "Resistance profile", "value": "Discordant", "direction": "contradicts"})
    else:
        signals.append({"signal": "Resistance profile", "value": "No resistance data", "direction": "missing"})

    # -- Epidemiological: Geography ---------------------------------------------
    if same_region:
        signals.append({"signal": "Geography", "value": f"Same region ({region_a})", "direction": "supports"})
    else:
        signals.append({"signal": "Geography", "value": f"Different regions ({region_a} / {region_b})", "direction": "neutral"})

    # -- Epidemiological: Temporal overlap --------------------------------------
    if temporal_delta_days is not None:
        if temporal_plausible:
            signals.append({"signal": "Time overlap", "value": f"{temporal_delta_days} days between specimens (within {temporal_window_days}d window)", "direction": "supports"})
        else:
            signals.append({"signal": "Time overlap", "value": f"{temporal_delta_days} days between specimens (exceeds {temporal_window_days}d window)", "direction": "contradicts"})
    else:
        signals.append({"signal": "Time overlap", "value": "Specimen dates missing", "direction": "missing"})

    # -- Clinical: Infectious period overlap ------------------------------------
    infectious_overlap = _infectious_period_overlap(row_a, row_b)
    if not infectious_overlap["available"]:
        signals.append({"signal": "Infectious period", "value": "Clinical data insufficient to estimate", "direction": "missing"})
    elif infectious_overlap["overlap"]:
        signals.append({"signal": "Infectious period", "value": f"Estimated windows overlap by ~{infectious_overlap['overlap_days']} days ({infectious_overlap['basis_a']} / {infectious_overlap['basis_b']})", "direction": "supports"})
    else:
        signals.append({"signal": "Infectious period", "value": f"Estimated windows do not overlap ({infectious_overlap['basis_a']} / {infectious_overlap['basis_b']})", "direction": "contradicts"})

    # -- Epidemiological: Shared exposure ---------------------------------------
    if epi_shared_locations > 0:
        domains_str = ", ".join(epi_domains) if epi_domains else "unclassified setting"
        signals.append({"signal": "Exposure overlap", "value": f"{epi_shared_locations} shared location(s) - {domains_str}", "direction": "supports"})
    else:
        signals.append({"signal": "Exposure overlap", "value": "No shared location events recorded", "direction": "missing"})

    # -- Epidemiological: Contact evidence --------------------------------------
    if epi_shared_contacts > 0:
        directness = "direct" if epi_shared_contacts >= 2 else "indirect"
        signals.append({"signal": "Contact evidence", "value": f"{epi_shared_contacts} shared contact(s) - {directness}", "direction": "supports"})
    else:
        signals.append({"signal": "Contact evidence", "value": "No shared contacts recorded", "direction": "missing"})

    # -- Clinical: Infectiousness -----------------------------------------------
    weight_a = _infectiousness_weight(row_a)
    weight_b = _infectiousness_weight(row_b)
    desc_a = _infectiousness_description(row_a)
    desc_b = _infectiousness_description(row_b)
    if weight_a > 0 or weight_b > 0:
        inf_notes = []
        if weight_a > 0:
            inf_notes.append(f"Case A: {desc_a}")
        if weight_b > 0:
            inf_notes.append(f"Case B: {desc_b}")
        signals.append({"signal": "Infectiousness", "value": "; ".join(inf_notes), "direction": "supports"})
    elif weight_a < 0 or weight_b < 0:
        signals.append({"signal": "Infectiousness", "value": "Low infectiousness indicators present", "direction": "neutral"})
    else:
        signals.append({"signal": "Infectiousness", "value": "Clinical infectiousness data not available", "direction": "missing"})

    # -- Contradictions summary -------------------------------------------------
    if contradictory_evidence:
        signals.append({"signal": "Contradictions", "value": "; ".join(sorted(set(contradictory_evidence))), "direction": "contradicts"})
    else:
        signals.append({"signal": "Contradictions", "value": "None identified", "direction": "neutral"})

    # -- Confidence label -------------------------------------------------------
    strong_threshold = int(confidence_thresholds.get("strong", 5))
    moderate_threshold = int(confidence_thresholds.get("moderate", 3))
    weak_threshold = int(confidence_thresholds.get("weak", 1))

    if overall_score >= strong_threshold:
        confidence = "strong"
    elif overall_score >= moderate_threshold:
        confidence = "moderate"
    elif overall_score >= weak_threshold:
        confidence = "weak"
    elif contradictory_evidence:
        confidence = "contradicted"
    else:
        confidence = "insufficient"

    supporting_count = sum(1 for s in signals if s["direction"] == "supports")
    contradicting_count = sum(1 for s in signals if s["direction"] == "contradicts")

    # -- Natural language interpretation ---------------------------------------
    intro_map = {
        "strong": "Strong genomic and epidemiological support for recent transmission.",
        "moderate": "Moderate genomic and epidemiological support for recent transmission.",
        "weak": "Weak or limited support for recent transmission.",
        "contradicted": "Contradictory evidence detected; direct transmission is unlikely.",
        "insufficient": "Insufficient evidence to determine transmission status.",
    }
    sentences = [intro_map[confidence]]

    if snp_distance is not None:
        if snp_distance <= snp_strong_threshold:
            sentences.append(f"SNP distance is {snp_distance}, below the strong linkage threshold of {snp_strong_threshold}.")
        elif snp_distance <= snp_moderate_threshold:
            sentences.append(f"SNP distance is {snp_distance} (moderate range, threshold {snp_moderate_threshold}).")
        else:
            sentences.append(f"SNP distance is {snp_distance}, which exceeds the genomic linkage threshold.")

    if lineage_a and lineage_b:
        if same_lineage:
            sentences.append(f"Lineage is matched ({lineage_a}).")
        else:
            sentences.append(f"Lineages differ ({lineage_a} vs {lineage_b}), inconsistent with a single transmission chain.")

    if resistance_concordance == "concordant":
        sentences.append("Resistance profiles are concordant.")
    elif resistance_concordance == "discordant":
        sentences.append("Resistance profiles are discordant, inconsistent with direct transmission.")

    if same_region:
        sentences.append(f"Both cases are in the same geographic region ({region_a}).")

    if temporal_delta_days is not None:
        if temporal_plausible:
            sentences.append(f"Specimen dates are {temporal_delta_days} days apart, within the plausible transmission window.")
        else:
            sentences.append(f"Specimen dates are {temporal_delta_days} days apart, exceeding the plausible transmission window.")

    if infectious_overlap["available"]:
        if infectious_overlap["overlap"]:
            sentences.append(f"Estimated infectious periods overlap by approximately {infectious_overlap['overlap_days']} days.")
        else:
            sentences.append("Estimated infectious periods do not overlap, which reduces transmission plausibility.")

    if epi_shared_locations > 0:
        domains_str = ", ".join(epi_domains) if epi_domains else "unclassified"
        sentences.append(f"Shared exposure has been recorded ({domains_str}).")
    if epi_shared_contacts > 0:
        sentences.append(f"{epi_shared_contacts} shared contact(s) have been identified.")

    if weight_a > 0:
        sentences.append(f"Case A has elevated infectiousness indicators ({desc_a}).")
    if weight_b > 0:
        sentences.append(f"Case B has elevated infectiousness indicators ({desc_b}).")

    if not contradictory_evidence:
        sentences.append("No major contradictory signals were identified.")
    else:
        sentences.append(f"Contradictory signals present: {'; '.join(sorted(set(contradictory_evidence)))}.")

    sentences.append(f"Data completeness score for this pair is {data_completeness_score:.2f}.")

    directionality = _directionality_hypothesis(row_a, row_b, infectious_overlap)

    recommendation_map = {
        "strong": "Immediate investigation and contact tracing are recommended. This pair should be prioritised for public health follow-up.",
        "moderate": "Contact tracing and further investigation are recommended. This pair warrants formal review.",
        "weak": "Review with additional supporting evidence. Current evidence is insufficient to confirm or exclude transmission.",
        "contradicted": "Transmission is unlikely given contradictory signals. Record findings for audit purposes.",
        "insufficient": "Await additional genomic or epidemiological data before drawing conclusions.",
    }

    return {
        "signals": signals,
        "confidence": confidence,
        "supporting_signal_count": supporting_count,
        "contradicting_signal_count": contradicting_count,
        "data_completeness_score": data_completeness_score,
        "contradiction_details": contradiction_details,
        "directionality_hypothesis": directionality,
        "interpretation": " ".join(sentences),
        "recommendation": recommendation_map[confidence],
    }


def _build_pair_epi_index(db: Session, case_ids: set[str]) -> dict[tuple[str, str], dict]:
    if len(case_ids) < 2:
        return {}

    location_rows = db.execute(text("""
        SELECT cle.case_id::text AS case_id,
               COALESCE(cle.location_id::text, '') AS location_id,
               COALESCE(LOWER(l.location_type), '') AS location_type,
               COALESCE(LOWER(e.exposure_type), '') AS exposure_type,
               COALESCE(LOWER(e.exposure_context), '') AS exposure_context,
               cle.arrived_at,
               cle.departed_at
        FROM case_location_events cle
        LEFT JOIN locations l ON l.location_id = cle.location_id
        LEFT JOIN exposures e ON e.exposure_id = cle.exposure_id
                WHERE COALESCE(cle.entered_in_error, FALSE) = FALSE
                    AND (l.location_id IS NULL OR COALESCE(l.entered_in_error, FALSE) = FALSE)
                    AND (e.exposure_id IS NULL OR COALESCE(e.entered_in_error, FALSE) = FALSE)
    """)).mappings().all()

    contact_rows = db.execute(text("""
         SELECT ccl.case_id::text AS case_id,
             ccl.contact_id::text AS contact_id
         FROM case_contact_links ccl
         LEFT JOIN contacts c ON c.contact_id = ccl.contact_id
         LEFT JOIN exposures e ON e.exposure_id = ccl.exposure_id
         WHERE COALESCE(ccl.entered_in_error, FALSE) = FALSE
           AND (c.contact_id IS NULL OR COALESCE(c.entered_in_error, FALSE) = FALSE)
           AND (e.exposure_id IS NULL OR COALESCE(e.entered_in_error, FALSE) = FALSE)
    """)).mappings().all()

    by_case_locations: dict[str, list[dict]] = defaultdict(list)
    for row in location_rows:
        case_id = str(row.get("case_id") or "")
        if case_id in case_ids:
            by_case_locations[case_id].append(dict(row))

    by_case_contacts: dict[str, set[str]] = defaultdict(set)
    for row in contact_rows:
        case_id = str(row.get("case_id") or "")
        contact_id = str(row.get("contact_id") or "")
        if case_id in case_ids and contact_id:
            by_case_contacts[case_id].add(contact_id)

    pair_index: dict[tuple[str, str], dict] = {}
    for case_a, case_b in combinations(sorted(case_ids), 2):
        shared_domains: set[str] = set()
        supports: list[str] = []
        direct_observation = False

        left_locs = by_case_locations.get(case_a, [])
        right_locs = by_case_locations.get(case_b, [])
        shared_locations = 0

        for left in left_locs:
            for right in right_locs:
                if left.get("location_id") and left.get("location_id") == right.get("location_id"):
                    shared_locations += 1
                    direct_observation = True
                    domain = _infer_epi_domain(
                        str(left.get("location_type") or right.get("location_type") or ""),
                        str(left.get("exposure_type") or right.get("exposure_type") or ""),
                        str(left.get("exposure_context") or right.get("exposure_context") or ""),
                    )
                    shared_domains.add(domain)

        shared_contacts = by_case_contacts.get(case_a, set()) & by_case_contacts.get(case_b, set())
        if shared_contacts:
            direct_observation = True
            shared_domains.add("social_exposure")

        if shared_locations > 0:
            supports.append(f"shared_locations:{shared_locations}")
        if shared_contacts:
            supports.append(f"shared_contacts:{len(shared_contacts)}")

        pair_index[_pair_key(case_a, case_b)] = {
            "domains": sorted(domain for domain in shared_domains if domain != "unknown"),
            "supports": supports,
            "direct_observation": direct_observation,
            "shared_location_count": shared_locations,
            "shared_contact_count": len(shared_contacts),
        }

    return pair_index


def _score_label(score: int) -> str:
    if score >= 5:
        return "strong epi support"
    if score >= 3:
        return "moderate epi support"
    if score >= 1:
        return "weak epi support"
    if score < 0:
        return "contradictory"
    return "unknown"


def _model_to_reviewer_classification(overall_interpretation: str) -> str:
    value = str(overall_interpretation or "").strip().lower()
    if value.startswith("strong support"):
        return "probable transmission"
    if value.startswith("moderate support"):
        return "possible transmission"
    if value.startswith("weak support"):
        return "possible transmission"
    if value.startswith("contradictory"):
        return "unlikely transmission"
    return "insufficient evidence"


def _calibration_summary(comparisons: list[dict]) -> dict:
    reviewed = len(comparisons)
    if reviewed == 0:
        return {
            "reviewed_pairs": 0,
            "exact_agreement": None,
            "binary_agreement": None,
            "binary_kappa": None,
            "by_label": {},
            "confusion": {},
        }

    supportive = {"confirmed transmission", "probable transmission", "possible transmission"}
    exact_matches = 0
    binary_matches = 0
    model_supportive_count = 0
    reviewer_supportive_count = 0
    by_label = defaultdict(lambda: {"count": 0, "exact_matches": 0})
    confusion = defaultdict(lambda: defaultdict(int))

    for row in comparisons:
        model_label = str(row.get("model_label") or "insufficient evidence")
        reviewer_label = str(row.get("reviewer_label") or "insufficient evidence")
        exact = model_label == reviewer_label
        if exact:
            exact_matches += 1

        model_supportive = model_label in supportive
        reviewer_supportive = reviewer_label in supportive
        if model_supportive:
            model_supportive_count += 1
        if reviewer_supportive:
            reviewer_supportive_count += 1
        if model_supportive == reviewer_supportive:
            binary_matches += 1

        by_label[reviewer_label]["count"] += 1
        if exact:
            by_label[reviewer_label]["exact_matches"] += 1
        confusion[reviewer_label][model_label] += 1

    observed = binary_matches / reviewed
    p_model_supportive = model_supportive_count / reviewed
    p_reviewer_supportive = reviewer_supportive_count / reviewed
    expected = (
        (p_model_supportive * p_reviewer_supportive)
        + ((1.0 - p_model_supportive) * (1.0 - p_reviewer_supportive))
    )
    if expected >= 0.9999:
        binary_kappa = None
    else:
        binary_kappa = round((observed - expected) / (1.0 - expected), 4)

    return {
        "reviewed_pairs": reviewed,
        "exact_agreement": round(exact_matches / reviewed, 4),
        "binary_agreement": round(binary_matches / reviewed, 4),
        "binary_kappa": binary_kappa,
        "by_label": {
            label: {
                "count": stats["count"],
                "exact_matches": stats["exact_matches"],
                "exact_agreement": round(stats["exact_matches"] / stats["count"], 4) if stats["count"] else None,
            }
            for label, stats in sorted(by_label.items())
        },
        "confusion": {
            reviewer_label: dict(sorted(predictions.items()))
            for reviewer_label, predictions in sorted(confusion.items())
        },
    }


def _calibration_by_confidence(comparisons: list[dict]) -> dict:
    tiers = defaultdict(lambda: {"count": 0, "exact_matches": 0})
    for row in comparisons:
        tier = str(row.get("model_confidence") or "unknown")
        tiers[tier]["count"] += 1
        if row.get("match"):
            tiers[tier]["exact_matches"] += 1
    return {
        tier: {
            "count": stats["count"],
            "exact_matches": stats["exact_matches"],
            "exact_agreement": round(stats["exact_matches"] / stats["count"], 4) if stats["count"] else None,
        }
        for tier, stats in sorted(tiers.items())
    }


def _build_case_pair_evidence_payload(
    rows: list,
    pair_epi_index: dict[tuple[str, str], dict],
    *,
    snp_strong_threshold: int,
    snp_moderate_threshold: int,
    temporal_window_days: int,
    max_pairs: int,
    scoring_profile: dict | None = None,
) -> dict:
    profile = scoring_profile or _resolve_scoring_profile(DEFAULT_SCORING_PROFILE)
    score_weights = profile.get("score_weights") or {}
    confidence_thresholds = profile.get("confidence_thresholds") or {}
    missing_data_penalty_max = int(profile.get("missing_data_penalty_max") or 0)

    rows_by_id = {str(r["case_id"]): r for r in rows if r.get("case_id")}
    ordered_case_ids = sorted(rows_by_id.keys(), key=lambda cid: str(rows_by_id[cid].get("specimen_date") or "9999-12-31"))

    pairs: list[dict] = []
    support_summary = defaultdict(int)

    for case_a, case_b in combinations(ordered_case_ids, 2):
        if len(pairs) >= max_pairs:
            break

        left = rows_by_id[case_a]
        right = rows_by_id[case_b]
        missing_evidence: list[str] = []
        supporting_evidence: list[str] = []
        contradictory_evidence: list[str] = []

        seq_a = str(left.get("sequence") or "")
        seq_b = str(right.get("sequence") or "")
        snp_validation = validated_snp_distance(seq_a, seq_b) if seq_a and seq_b else None
        snp_distance = snp_validation.distance if snp_validation else None

        lineage_a = str(left.get("lineage") or "")
        lineage_b = str(right.get("lineage") or "")
        same_lineage = bool(lineage_a and lineage_b and lineage_a == lineage_b)

        if snp_distance is None:
            genomic_support = "unknown"
            missing_evidence.append("missing_sequence")
        elif snp_distance <= snp_strong_threshold:
            genomic_support = "strong epi support"
            supporting_evidence.append(f"low_snp_distance:{snp_distance}")
        elif snp_distance <= snp_moderate_threshold:
            genomic_support = "moderate epi support"
            supporting_evidence.append(f"moderate_snp_distance:{snp_distance}")
        elif snp_distance <= 20:
            genomic_support = "weak epi support"
            supporting_evidence.append(f"elevated_snp_distance:{snp_distance}")
        else:
            genomic_support = "contradictory"
            contradictory_evidence.append(f"high_snp_distance:{snp_distance}")

        if lineage_a and lineage_b:
            if same_lineage:
                supporting_evidence.append("same_lineage")
            else:
                contradictory_evidence.append("lineage_mismatch")
        else:
            missing_evidence.append("missing_lineage")

        specimen_a = left.get("specimen_date")
        specimen_b = right.get("specimen_date")
        temporal_delta_days = None
        if specimen_a and specimen_b:
            temporal_delta_days = abs((specimen_b - specimen_a).days)
            temporal_plausible = temporal_delta_days <= temporal_window_days
            if temporal_plausible:
                supporting_evidence.append(f"temporal_overlap_within_{temporal_window_days}d")
            else:
                contradictory_evidence.append(f"temporal_gap_exceeds_{temporal_window_days}d")
        else:
            temporal_plausible = None
            missing_evidence.append("missing_specimen_date")

        infectious_overlap = _infectious_period_overlap(left, right)
        if infectious_overlap.get("available") and not infectious_overlap.get("overlap"):
            contradictory_evidence.append("infectious_period_non_overlap")

        region_a = str(left.get("region") or "Unknown")
        region_b = str(right.get("region") or "Unknown")
        same_region = region_a == region_b and region_a != "Unknown"
        if same_region:
            supporting_evidence.append("same_geographic_region")

        epi = pair_epi_index.get(_pair_key(case_a, case_b), {})
        epi_domains = epi.get("domains") or []
        supporting_evidence.extend(epi.get("supports") or [])

        epi_score = 0
        if epi.get("shared_contact_count", 0) > 0:
            epi_score += 3
        if epi.get("shared_location_count", 0) > 0:
            epi_score += 2
        if same_region:
            epi_score += 1
        if temporal_plausible is True:
            epi_score += 1
        if temporal_plausible is False:
            epi_score -= 1

        epi_support = _score_label(epi_score)
        if not epi_domains and not same_region:
            missing_evidence.append("no_recorded_epi_link")

        resistant_a = _resistant_drug_set(left.get("predicted_drug_resistance"))
        resistant_b = _resistant_drug_set(right.get("predicted_drug_resistance"))
        if resistant_a or resistant_b:
            if resistant_a == resistant_b:
                resistance_concordance = "concordant"
                supporting_evidence.append("resistance_profile_concordant")
            elif resistant_a & resistant_b:
                resistance_concordance = "partially_concordant"
                supporting_evidence.append("resistance_profile_partial_overlap")
            else:
                resistance_concordance = "discordant"
                contradictory_evidence.append("resistance_profile_discordant")
        else:
            resistance_concordance = "unknown"
            missing_evidence.append("missing_resistance_profile")

        basis_parts = []
        if genomic_support != "unknown":
            basis_parts.append("genomic")
        if epi_support != "unknown":
            basis_parts.append("epi")
        evidence_basis = "+".join(basis_parts) if basis_parts else "insufficient"

        observation_mode = "directly_observed" if epi.get("direct_observation") else "inferred"

        overall_score = 0
        overall_score += int(score_weights.get(genomic_support, 0))
        overall_score += int(score_weights.get(epi_support, 0))
        if resistance_concordance == "concordant":
            overall_score += 1
        elif resistance_concordance == "discordant":
            overall_score -= 1

        data_completeness_score = _pair_data_completeness_score(left, right)
        uncertainty_penalty = round((1.0 - data_completeness_score) * missing_data_penalty_max)
        if uncertainty_penalty > 0:
            overall_score -= uncertainty_penalty
            missing_evidence.append(f"data_completeness_penalty:{uncertainty_penalty}")

        contradiction_details = _contradiction_details(contradictory_evidence)

        strong_threshold = int(confidence_thresholds.get("strong", 5))
        moderate_threshold = int(confidence_thresholds.get("moderate", 3))
        weak_threshold = int(confidence_thresholds.get("weak", 1))

        if overall_score >= strong_threshold:
            overall_interpretation = "strong support, needs reviewer confirmation"
        elif overall_score >= moderate_threshold:
            overall_interpretation = "moderate support, needs reviewer confirmation"
        elif overall_score >= weak_threshold:
            overall_interpretation = "weak support, review with caution"
        elif contradictory_evidence:
            overall_interpretation = "contradictory evidence"
        else:
            overall_interpretation = "insufficient evidence"

        support_summary[overall_interpretation] += 1

        evidence_card = _build_evidence_card(
            snp_distance=snp_distance,
            snp_strong_threshold=snp_strong_threshold,
            snp_moderate_threshold=snp_moderate_threshold,
            lineage_a=lineage_a,
            lineage_b=lineage_b,
            same_lineage=same_lineage,
            resistance_concordance=resistance_concordance,
            same_region=same_region,
            region_a=region_a,
            region_b=region_b,
            temporal_delta_days=temporal_delta_days,
            temporal_plausible=temporal_plausible,
            temporal_window_days=temporal_window_days,
            epi_domains=epi_domains,
            epi_shared_locations=int(epi.get("shared_location_count") or 0),
            epi_shared_contacts=int(epi.get("shared_contact_count") or 0),
            row_a=left,
            row_b=right,
            overall_score=overall_score,
            contradictory_evidence=contradictory_evidence,
            contradiction_details=contradiction_details,
            data_completeness_score=data_completeness_score,
            confidence_thresholds=confidence_thresholds,
        )

        pairs.append(
            {
                "case_a": case_a,
                "case_b": case_b,
                "pair": f"{case_a[:8]} -> {case_b[:8]}",
                "genomic_plausibility": {
                    "support": genomic_support,
                    "snp_distance": snp_distance,
                    "snp_validation_status": snp_validation.status if snp_validation else "missing_sequence",
                    "same_lineage": same_lineage,
                    "lineage_a": lineage_a or None,
                    "lineage_b": lineage_b or None,
                    "resistance_profile": resistance_concordance,
                },
                "temporal_plausibility": {
                    "window_days": temporal_window_days,
                    "delta_days": temporal_delta_days,
                    "plausible": temporal_plausible,
                    "specimen_date_a": str(specimen_a) if specimen_a else None,
                    "specimen_date_b": str(specimen_b) if specimen_b else None,
                },
                "epidemiological_support": {
                    "support": epi_support,
                    "domains": epi_domains,
                    "same_region": same_region,
                    "shared_location_count": int(epi.get("shared_location_count") or 0),
                    "shared_contact_count": int(epi.get("shared_contact_count") or 0),
                },
                "evidence": {
                    "supports": sorted(set(supporting_evidence)),
                    "missing": sorted(set(missing_evidence)),
                    "contradicts": sorted(set(contradictory_evidence)),
                    "contradiction_details": contradiction_details,
                    "data_completeness_score": data_completeness_score,
                    "basis": evidence_basis,
                    "observation_mode": observation_mode,
                },
                "overall_interpretation": overall_interpretation,
                "evidence_card": evidence_card,
                "reviewer_classification": None,
            }
        )

    return {
        "pair_count": len(pairs),
        "support_summary": dict(support_summary),
        "scoring_profile_version": str(profile.get("version") or "default_v1"),
        "pairs": pairs,
        "reviewer_classification_options": list(REVIEW_CLASSIFICATIONS),
    }


@router.get("/case-pair-evidence")
def case_pair_evidence(
    cluster_id: str | None = Query(None),
    max_cases: int = Query(40, ge=2, le=120),
    max_pairs: int = Query(250, ge=1, le=2000),
    scoring_profile: str = "default_v1",
    snp_strong_threshold: int = Query(5, ge=0, le=100),
    snp_moderate_threshold: int = Query(12, ge=1, le=200),
    temporal_window_days: int = Query(90, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """Build structured transmission-link evidence for case pairs."""
    rows = _case_rows(db)
    if cluster_id:
        cid = _normalise_uuid(cluster_id)
        rows = [r for r in rows if str(r.get("cluster_id") or "") == cid]

    rows.sort(key=lambda r: str(r.get("specimen_date") or "9999-12-31"))
    rows = rows[:max_cases]
    profile = _resolve_scoring_profile(scoring_profile)

    case_ids = {str(r.get("case_id")) for r in rows if r.get("case_id")}
    pair_epi_index = _build_pair_epi_index(db, case_ids)
    payload = _build_case_pair_evidence_payload(
        rows,
        pair_epi_index,
        snp_strong_threshold=snp_strong_threshold,
        snp_moderate_threshold=snp_moderate_threshold,
        temporal_window_days=temporal_window_days,
        max_pairs=max_pairs,
        scoring_profile=profile,
    )

    pair_keys = {_pair_key(str(p["case_a"]), str(p["case_b"])) for p in payload.get("pairs", [])}
    reviews = _latest_pair_review_map(db, pair_keys)
    reviewed_count = 0
    for pair in payload.get("pairs", []):
        review = reviews.get(_pair_key(str(pair["case_a"]), str(pair["case_b"])))
        if review:
            pair["reviewer_classification"] = review
            reviewed_count += 1

    payload["parameters"] = {
        "cluster_id": cluster_id,
        "max_cases": max_cases,
        "max_pairs": max_pairs,
        "scoring_profile": scoring_profile,
        "scoring_profile_version": str(profile.get("version") or "default_v1"),
        "snp_strong_threshold": snp_strong_threshold,
        "snp_moderate_threshold": snp_moderate_threshold,
        "temporal_window_days": temporal_window_days,
    }
    payload["case_count"] = len(rows)
    payload["reviewed_pair_count"] = reviewed_count
    payload["generated_at"] = datetime.utcnow().isoformat() + "Z"
    payload["notes"] = [
        "Heuristic evidence synthesis for reviewer support; not a validated transmission model.",
        "SNP distance is supportive context only and not proof of direct transmission.",
    ]
    payload.update(_validation_notice())
    return payload


@router.get("/transmission-evidence/{case_a_id}/{case_b_id}")
def transmission_evidence(
    case_a_id: str,
    case_b_id: str,
    scoring_profile: str = "default_v1",
    snp_strong_threshold: int = Query(5, ge=0, le=100),
    snp_moderate_threshold: int = Query(12, ge=1, le=200),
    temporal_window_days: int = Query(90, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """Return a full structured transmission evidence card for a single case pair."""
    norm_a = _normalise_uuid(case_a_id)
    norm_b = _normalise_uuid(case_b_id)
    if norm_a == norm_b:
        raise HTTPException(status_code=422, detail="case_a_id and case_b_id must be different cases")

    rows = _case_rows(db)
    rows_by_id = {str(r["case_id"]): r for r in rows if r.get("case_id")}

    row_a = rows_by_id.get(norm_a)
    row_b = rows_by_id.get(norm_b)
    if not row_a:
        raise HTTPException(status_code=404, detail=f"Case {norm_a} not found")
    if not row_b:
        raise HTTPException(status_code=404, detail=f"Case {norm_b} not found")

    case_ids = {norm_a, norm_b}
    pair_epi_index = _build_pair_epi_index(db, case_ids)
    epi = pair_epi_index.get(_pair_key(norm_a, norm_b), {})
    epi_domains = epi.get("domains") or []

    seq_a = str(row_a.get("sequence") or "")
    seq_b = str(row_b.get("sequence") or "")
    snp_validation = validated_snp_distance(seq_a, seq_b) if seq_a and seq_b else None
    snp_distance = snp_validation.distance if snp_validation else None

    lineage_a = str(row_a.get("lineage") or "")
    lineage_b = str(row_b.get("lineage") or "")
    same_lineage = bool(lineage_a and lineage_b and lineage_a == lineage_b)

    specimen_a = row_a.get("specimen_date")
    specimen_b = row_b.get("specimen_date")
    temporal_delta_days = abs((specimen_b - specimen_a).days) if specimen_a and specimen_b else None
    temporal_plausible = (temporal_delta_days <= temporal_window_days) if temporal_delta_days is not None else None

    region_a = str(row_a.get("region") or "Unknown")
    region_b = str(row_b.get("region") or "Unknown")
    same_region = region_a == region_b and region_a != "Unknown"

    resistant_a = _resistant_drug_set(row_a.get("predicted_drug_resistance"))
    resistant_b = _resistant_drug_set(row_b.get("predicted_drug_resistance"))
    if resistant_a or resistant_b:
        if resistant_a == resistant_b:
            resistance_concordance = "concordant"
        elif resistant_a & resistant_b:
            resistance_concordance = "partially_concordant"
        else:
            resistance_concordance = "discordant"
    else:
        resistance_concordance = "unknown"

    contradictory_evidence: list[str] = []
    if snp_distance is not None and snp_distance > 20:
        contradictory_evidence.append(f"high_snp_distance:{snp_distance}")
    if lineage_a and lineage_b and not same_lineage:
        contradictory_evidence.append("lineage_mismatch")
    if temporal_plausible is False:
        contradictory_evidence.append(f"temporal_gap_exceeds_{temporal_window_days}d")
    if resistance_concordance == "discordant":
        contradictory_evidence.append("resistance_profile_discordant")

    epi_score = 0
    if epi.get("shared_contact_count", 0) > 0:
        epi_score += 3
    if epi.get("shared_location_count", 0) > 0:
        epi_score += 2
    if same_region:
        epi_score += 1
    if temporal_plausible is True:
        epi_score += 1
    if temporal_plausible is False:
        epi_score -= 1

    profile = _resolve_scoring_profile(scoring_profile)
    score_map = profile.get("score_weights") or {}
    confidence_thresholds = profile.get("confidence_thresholds") or {}
    missing_data_penalty_max = int(profile.get("missing_data_penalty_max") or 0)

    genomic_support_label = (
        "strong epi support" if snp_distance is not None and snp_distance <= snp_strong_threshold
        else "moderate epi support" if snp_distance is not None and snp_distance <= snp_moderate_threshold
        else "weak epi support" if snp_distance is not None and snp_distance <= 20
        else "contradictory" if snp_distance is not None
        else "unknown"
    )
    overall_score = int(score_map.get(genomic_support_label, 0)) + int(score_map.get(_score_label(epi_score), 0))
    if resistance_concordance == "concordant":
        overall_score += 1
    elif resistance_concordance == "discordant":
        overall_score -= 1

    infectious_overlap = _infectious_period_overlap(row_a, row_b)
    if infectious_overlap.get("available") and not infectious_overlap.get("overlap"):
        contradictory_evidence.append("infectious_period_non_overlap")

    data_completeness_score = _pair_data_completeness_score(row_a, row_b)
    uncertainty_penalty = round((1.0 - data_completeness_score) * missing_data_penalty_max)
    if uncertainty_penalty > 0:
        overall_score -= uncertainty_penalty
    contradiction_details = _contradiction_details(contradictory_evidence)

    card = _build_evidence_card(
        snp_distance=snp_distance,
        snp_strong_threshold=snp_strong_threshold,
        snp_moderate_threshold=snp_moderate_threshold,
        lineage_a=lineage_a,
        lineage_b=lineage_b,
        same_lineage=same_lineage,
        resistance_concordance=resistance_concordance,
        same_region=same_region,
        region_a=region_a,
        region_b=region_b,
        temporal_delta_days=temporal_delta_days,
        temporal_plausible=temporal_plausible,
        temporal_window_days=temporal_window_days,
        epi_domains=epi_domains,
        epi_shared_locations=int(epi.get("shared_location_count") or 0),
        epi_shared_contacts=int(epi.get("shared_contact_count") or 0),
        row_a=row_a,
        row_b=row_b,
        overall_score=overall_score,
        contradictory_evidence=contradictory_evidence,
        contradiction_details=contradiction_details,
        data_completeness_score=data_completeness_score,
        confidence_thresholds=confidence_thresholds,
    )

    return {
        "case_a": norm_a,
        "case_b": norm_b,
        "pair": f"{norm_a[:8]} -> {norm_b[:8]}",
        "parameters": {
            "scoring_profile": scoring_profile,
            "scoring_profile_version": str(profile.get("version") or "default_v1"),
            "snp_strong_threshold": snp_strong_threshold,
            "snp_moderate_threshold": snp_moderate_threshold,
            "temporal_window_days": temporal_window_days,
        },
        "evidence_card": card,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        **_validation_notice(),
    }


@router.post("/case-pair-review")
def upsert_case_pair_review(
    payload: CasePairReviewUpsert,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("analyst")),
):
    """Create or update reviewer classification for a case-pair."""
    left = _normalise_uuid(payload.case_a)
    right = _normalise_uuid(payload.case_b)
    if left == right:
        raise HTTPException(status_code=422, detail="case_a and case_b must be different cases")

    case_a, case_b = _pair_key(left, right)
    cluster_id = _normalise_uuid(payload.cluster_id) if payload.cluster_id else None
    reviewer = (payload.reviewer or user.subject).strip()

    db.execute(text("""
        INSERT INTO case_pair_reviews (
            case_a,
            case_b,
            reviewer_classification,
            reviewer,
            notes,
            source_cluster_id,
            entered_in_error,
            entered_in_error_at,
            entered_in_error_by,
            entered_in_error_reason,
            reviewed_at
        )
        VALUES (
            CAST(:case_a AS UUID),
            CAST(:case_b AS UUID),
            :classification,
            :reviewer,
            :notes,
            CAST(:cluster_id AS UUID),
            FALSE,
            NULL,
            NULL,
            NULL,
            NOW()
        )
        ON CONFLICT (case_a, case_b)
        DO UPDATE SET
            reviewer_classification = EXCLUDED.reviewer_classification,
            reviewer = EXCLUDED.reviewer,
            notes = EXCLUDED.notes,
            source_cluster_id = EXCLUDED.source_cluster_id,
            entered_in_error = FALSE,
            entered_in_error_at = NULL,
            entered_in_error_by = NULL,
            entered_in_error_reason = NULL,
            reviewed_at = NOW()
    """), {
        "case_a": case_a,
        "case_b": case_b,
        "classification": payload.reviewer_classification,
        "reviewer": reviewer,
        "notes": payload.notes,
        "cluster_id": cluster_id,
    })

    db.execute(
        text(
            """
            INSERT INTO audit_log (action, user_id, details, timestamp)
            VALUES (
                :action,
                :user_id,
                CAST(:details AS JSONB),
                NOW()
            )
            """
        ),
        {
            "action": "case_pair_review_upserted",
            "user_id": reviewer,
            "details": json.dumps(
                {
                    "case_a": case_a,
                    "case_b": case_b,
                    "reviewer_classification": payload.reviewer_classification,
                    "cluster_id": cluster_id,
                }
            ),
        },
    )
    db.commit()

    return {
        "case_a": case_a,
        "case_b": case_b,
        "reviewer_classification": payload.reviewer_classification,
        "reviewer": reviewer,
        "notes": payload.notes,
        "cluster_id": cluster_id,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }


@router.get("/case-pair-reviews")
def list_case_pair_reviews(
    cluster_id: str | None = Query(None),
    case_id: str | None = Query(None),
    include_entered_in_error: bool = Query(False),
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    """List saved reviewer classifications for case-pairs."""
    where = []
    params: dict[str, object] = {"limit": limit}

    if cluster_id:
        where.append("source_cluster_id = CAST(:cluster_id AS UUID)")
        params["cluster_id"] = _normalise_uuid(cluster_id)
    if case_id:
        norm_case = _normalise_uuid(case_id)
        where.append("(case_a = CAST(:case_id AS UUID) OR case_b = CAST(:case_id AS UUID))")
        params["case_id"] = norm_case
    if not include_entered_in_error:
        where.append("COALESCE(entered_in_error, FALSE) = FALSE")

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT case_a::text AS case_a,
               case_b::text AS case_b,
               reviewer_classification,
               reviewer,
               notes,
               source_cluster_id::text AS cluster_id,
             COALESCE(entered_in_error, FALSE) AS entered_in_error,
             entered_in_error_at,
             entered_in_error_by,
             entered_in_error_reason,
               reviewed_at
        FROM case_pair_reviews
        {where_clause}
        ORDER BY reviewed_at DESC
        LIMIT :limit
    """
    rows = db.execute(text(sql), params).mappings().all()

    return {
        "total": len(rows),
        "reviews": [
            {
                "case_a": str(r.get("case_a") or ""),
                "case_b": str(r.get("case_b") or ""),
                "pair": f"{str(r.get('case_a') or '')[:8]} -> {str(r.get('case_b') or '')[:8]}",
                "reviewer_classification": str(r.get("reviewer_classification") or ""),
                "reviewer": str(r.get("reviewer") or ""),
                "notes": r.get("notes"),
                "cluster_id": str(r.get("cluster_id") or "") or None,
                "entered_in_error": bool(r.get("entered_in_error")),
                "entered_in_error_at": r.get("entered_in_error_at").isoformat() if r.get("entered_in_error_at") else None,
                "entered_in_error_by": str(r.get("entered_in_error_by") or "") or None,
                "entered_in_error_reason": r.get("entered_in_error_reason"),
                "reviewed_at": r.get("reviewed_at").isoformat() if r.get("reviewed_at") else None,
            }
            for r in rows
        ],
        "reviewer_classification_options": list(REVIEW_CLASSIFICATIONS),
    }


@router.post("/case-pair-review/entered-in-error")
def mark_case_pair_review_entered_in_error(
    payload: CasePairReviewEnteredInError,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("analyst")),
):
    left = _normalise_uuid(payload.case_a)
    right = _normalise_uuid(payload.case_b)
    if left == right:
        raise HTTPException(status_code=422, detail="case_a and case_b must be different cases")

    case_a, case_b = _pair_key(left, right)
    reviewer = (payload.reviewer or user.subject).strip()

    result = db.execute(
        text(
            """
            UPDATE case_pair_reviews
            SET entered_in_error = TRUE,
                entered_in_error_at = NOW(),
                entered_in_error_by = :reviewer,
                entered_in_error_reason = :reason,
                reviewed_at = NOW()
            WHERE case_a = CAST(:case_a AS UUID)
              AND case_b = CAST(:case_b AS UUID)
            """
        ),
        {
            "case_a": case_a,
            "case_b": case_b,
            "reviewer": reviewer,
            "reason": payload.reason.strip(),
        },
    )
    if (result.rowcount or 0) == 0:
        raise HTTPException(status_code=404, detail="Case pair review not found")

    db.execute(
        text(
            """
            INSERT INTO audit_log (action, user_id, details, timestamp)
            VALUES (:action, :user_id, CAST(:details AS JSONB), NOW())
            """
        ),
        {
            "action": "case_pair_review_entered_in_error",
            "user_id": reviewer,
            "details": json.dumps(
                {
                    "case_a": case_a,
                    "case_b": case_b,
                    "reason": payload.reason.strip(),
                }
            ),
        },
    )
    db.commit()

    return {
        "case_a": case_a,
        "case_b": case_b,
        "entered_in_error": True,
        "entered_in_error_reason": payload.reason.strip(),
        "entered_in_error_by": reviewer,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }


@router.post("/case-pair-review/restore")
def restore_case_pair_review(
    payload: CasePairReviewRestore,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("analyst")),
):
    left = _normalise_uuid(payload.case_a)
    right = _normalise_uuid(payload.case_b)
    if left == right:
        raise HTTPException(status_code=422, detail="case_a and case_b must be different cases")

    case_a, case_b = _pair_key(left, right)
    reviewer = (payload.reviewer or user.subject).strip()

    result = db.execute(
        text(
            """
            UPDATE case_pair_reviews
            SET entered_in_error = FALSE,
                entered_in_error_at = NULL,
                entered_in_error_by = NULL,
                entered_in_error_reason = NULL,
                reviewed_at = NOW()
            WHERE case_a = CAST(:case_a AS UUID)
              AND case_b = CAST(:case_b AS UUID)
            """
        ),
        {
            "case_a": case_a,
            "case_b": case_b,
        },
    )
    if (result.rowcount or 0) == 0:
        raise HTTPException(status_code=404, detail="Case pair review not found")

    db.execute(
        text(
            """
            INSERT INTO audit_log (action, user_id, details, timestamp)
            VALUES (:action, :user_id, CAST(:details AS JSONB), NOW())
            """
        ),
        {
            "action": "case_pair_review_restored",
            "user_id": reviewer,
            "details": json.dumps(
                {
                    "case_a": case_a,
                    "case_b": case_b,
                }
            ),
        },
    )
    db.commit()

    return {
        "case_a": case_a,
        "case_b": case_b,
        "entered_in_error": False,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }


@router.get("/cluster-why/{cluster_id}")
def cluster_why_it_matters(
    cluster_id: str,
    recent_days: int = Query(90, ge=7, le=365),
    resistance_drug_keyword: str = Query("rifamp", min_length=3, max_length=30),
    db: Session = Depends(get_db),
):
    """Explain why a cluster is prioritised, with explicit evidence bullets."""
    cid = _normalise_uuid(cluster_id)
    rows = [r for r in _case_rows(db) if str(r.get("cluster_id") or "") == cid]
    if not rows:
        raise HTTPException(status_code=404, detail="Cluster not found or has no cases")

    case_ids = {str(r.get("case_id")) for r in rows if r.get("case_id")}
    pair_epi_index = _build_pair_epi_index(db, case_ids)
    payload = _cluster_priority_reasons(
        rows,
        pair_epi_index,
        cid,
        recent_days=recent_days,
        resistance_drug_keyword=resistance_drug_keyword,
    )

    payload["summary"] = "This cluster is prioritised because: " + "; ".join(payload["reasons"])
    payload["generated_at"] = datetime.utcnow().isoformat() + "Z"
    payload.update(_validation_notice())
    return payload


def _run_case_pair_calibration(
    db: Session,
    *,
    cluster_id: str | None,
    max_cases: int,
    max_pairs: int,
    scoring_profile_name: str,
    scoring_profile: dict,
    snp_strong_threshold: int,
    snp_moderate_threshold: int,
    temporal_window_days: int,
) -> dict:
    rows = _case_rows(db)
    if cluster_id:
        cid = _normalise_uuid(cluster_id)
        rows = [r for r in rows if str(r.get("cluster_id") or "") == cid]

    rows.sort(key=lambda r: str(r.get("specimen_date") or "9999-12-31"))
    rows = rows[:max_cases]
    case_ids = {str(r.get("case_id")) for r in rows if r.get("case_id")}
    pair_epi_index = _build_pair_epi_index(db, case_ids)
    evidence_payload = _build_case_pair_evidence_payload(
        rows,
        pair_epi_index,
        snp_strong_threshold=snp_strong_threshold,
        snp_moderate_threshold=snp_moderate_threshold,
        temporal_window_days=temporal_window_days,
        max_pairs=max_pairs,
        scoring_profile=scoring_profile,
    )

    pairs = evidence_payload.get("pairs") or []
    pair_keys = {_pair_key(str(p.get("case_a") or ""), str(p.get("case_b") or "")) for p in pairs}
    reviews = _latest_pair_review_map(db, pair_keys)

    comparisons = []
    for pair in pairs:
        key = _pair_key(str(pair.get("case_a") or ""), str(pair.get("case_b") or ""))
        review = reviews.get(key)
        if not review:
            continue
        model_label = _model_to_reviewer_classification(str(pair.get("overall_interpretation") or ""))
        reviewer_label = str(review.get("classification") or "").strip().lower()
        comparisons.append(
            {
                "pair": pair.get("pair"),
                "case_a": pair.get("case_a"),
                "case_b": pair.get("case_b"),
                "model_interpretation": pair.get("overall_interpretation"),
                "model_confidence": (((pair.get("evidence_card") or {}).get("confidence")) or "unknown"),
                "model_label": model_label,
                "reviewer_label": reviewer_label,
                "match": model_label == reviewer_label,
                "reviewer": review.get("reviewer"),
                "reviewed_at": review.get("reviewed_at"),
            }
        )

    timeline_counts = defaultdict(int)
    for item in comparisons:
        ts = str(item.get("reviewed_at") or "")
        month = ts[:7] if len(ts) >= 7 else "unknown"
        timeline_counts[month] += 1

    summary = _calibration_summary(comparisons)
    by_confidence = _calibration_by_confidence(comparisons)

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "parameters": {
            "cluster_id": cluster_id,
            "max_cases": max_cases,
            "max_pairs": max_pairs,
            "scoring_profile": scoring_profile_name,
            "scoring_profile_version": str(scoring_profile.get("version") or scoring_profile_name),
            "snp_strong_threshold": snp_strong_threshold,
            "snp_moderate_threshold": snp_moderate_threshold,
            "temporal_window_days": temporal_window_days,
        },
        "model_pair_count": len(pairs),
        "reviewed_pair_count": len(comparisons),
        "coverage": round(len(comparisons) / len(pairs), 4) if pairs else None,
        "summary": summary,
        "summary_by_confidence": by_confidence,
        "review_volume_by_month": [
            {"month": month, "count": count}
            for month, count in sorted(timeline_counts.items())
        ],
        "comparisons": comparisons[:200],
        "reviewer_classification_options": list(REVIEW_CLASSIFICATIONS),
        "notes": [
            "Model labels are mapped from heuristic interpretations and require calibration before operational claims.",
            "Agreement values are descriptive quality metrics, not external validation evidence.",
        ],
        **_validation_notice(),
    }


def _parse_int_options(raw: str, *, minimum: int, maximum: int, field_name: str) -> list[int]:
    values: list[int] = []
    for token in str(raw or "").split(","):
        item = token.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid integer in {field_name}: '{item}'") from exc
        if value < minimum or value > maximum:
            raise HTTPException(
                status_code=422,
                detail=f"{field_name} values must be between {minimum} and {maximum}",
            )
        values.append(value)
    if not values:
        raise HTTPException(status_code=422, detail=f"{field_name} must include at least one integer")
    return sorted(set(values))


@router.get("/case-pair-calibration")
def case_pair_calibration(
    cluster_id: str | None = Query(None),
    max_cases: int = Query(80, ge=2, le=300),
    max_pairs: int = Query(1000, ge=1, le=10000),
    scoring_profile: str = "default_v1",
    snp_strong_threshold: int = Query(5, ge=0, le=100),
    snp_moderate_threshold: int = Query(12, ge=1, le=200),
    temporal_window_days: int = Query(90, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """Compare model pair interpretations with reviewer classifications."""
    profile = _resolve_scoring_profile(scoring_profile)
    return _run_case_pair_calibration(
        db,
        cluster_id=cluster_id,
        max_cases=max_cases,
        max_pairs=max_pairs,
        scoring_profile_name=scoring_profile,
        scoring_profile=profile,
        snp_strong_threshold=snp_strong_threshold,
        snp_moderate_threshold=snp_moderate_threshold,
        temporal_window_days=temporal_window_days,
    )


@router.get("/case-pair-calibration/sweep")
def case_pair_calibration_sweep(
    cluster_id: str | None = Query(None),
    max_cases: int = Query(80, ge=2, le=300),
    max_pairs: int = Query(1000, ge=1, le=10000),
    base_scoring_profile: str = "default_v1",
    snp_strong_threshold_options: str = Query("4,5,6"),
    snp_moderate_threshold_options: str = Query("10,12,14"),
    temporal_window_options: str = Query("30,45,60"),
    strong_epi_weight_options: str = Query("2,3,4"),
    contradiction_weight_options: str = Query("-2,-3,-4"),
    min_reviewed_pairs: int = Query(25, ge=1, le=5000),
    max_candidates: int = Query(100, ge=1, le=400),
    db: Session = Depends(get_db),
):
    """Run an automated sweep of scoring parameters and return the best calibration candidate."""
    base = _resolve_scoring_profile(base_scoring_profile)

    strong_thresholds = _parse_int_options(
        snp_strong_threshold_options,
        minimum=0,
        maximum=100,
        field_name="snp_strong_threshold_options",
    )
    moderate_thresholds = _parse_int_options(
        snp_moderate_threshold_options,
        minimum=1,
        maximum=200,
        field_name="snp_moderate_threshold_options",
    )
    temporal_windows = _parse_int_options(
        temporal_window_options,
        minimum=1,
        maximum=365,
        field_name="temporal_window_options",
    )
    strong_weights = _parse_int_options(
        strong_epi_weight_options,
        minimum=1,
        maximum=8,
        field_name="strong_epi_weight_options",
    )
    contradiction_weights = _parse_int_options(
        contradiction_weight_options,
        minimum=-12,
        maximum=-1,
        field_name="contradiction_weight_options",
    )

    candidates = []
    evaluated = 0
    for strong_threshold in strong_thresholds:
        for moderate_threshold in moderate_thresholds:
            if strong_threshold >= moderate_threshold:
                continue
            for temporal_window in temporal_windows:
                for strong_weight in strong_weights:
                    for contradiction_weight in contradiction_weights:
                        if evaluated >= max_candidates:
                            break
                        evaluated += 1
                        score_weights = dict(base.get("score_weights") or {})
                        score_weights["strong epi support"] = strong_weight
                        score_weights["moderate epi support"] = max(1, strong_weight - 1)
                        score_weights["weak epi support"] = max(1, strong_weight - 2)
                        score_weights["contradictory"] = contradiction_weight

                        profile = {
                            **base,
                            "version": (
                                f"sweep_st{strong_threshold}_mt{moderate_threshold}_tw{temporal_window}_"
                                f"sw{strong_weight}_cw{abs(contradiction_weight)}"
                            ),
                            "score_weights": score_weights,
                        }

                        result = _run_case_pair_calibration(
                            db,
                            cluster_id=cluster_id,
                            max_cases=max_cases,
                            max_pairs=max_pairs,
                            scoring_profile_name=profile["version"],
                            scoring_profile=profile,
                            snp_strong_threshold=strong_threshold,
                            snp_moderate_threshold=moderate_threshold,
                            temporal_window_days=temporal_window,
                        )

                        summary = result.get("summary") or {}
                        binary_kappa = summary.get("binary_kappa")
                        reviewed_pair_count = int(result.get("reviewed_pair_count") or 0)
                        coverage = result.get("coverage")
                        candidates.append(
                            {
                                "profile_version": profile["version"],
                                "snp_strong_threshold": strong_threshold,
                                "snp_moderate_threshold": moderate_threshold,
                                "temporal_window_days": temporal_window,
                                "score_weights": score_weights,
                                "reviewed_pair_count": reviewed_pair_count,
                                "coverage": coverage,
                                "binary_kappa": binary_kappa,
                                "binary_agreement": summary.get("binary_agreement"),
                                "exact_agreement": summary.get("exact_agreement"),
                            }
                        )
                    if evaluated >= max_candidates:
                        break
                if evaluated >= max_candidates:
                    break
            if evaluated >= max_candidates:
                break
        if evaluated >= max_candidates:
            break

    eligible = [c for c in candidates if c["reviewed_pair_count"] >= min_reviewed_pairs and c["binary_kappa"] is not None]
    if not eligible:
        eligible = [c for c in candidates if c["binary_kappa"] is not None]

    best_candidate = None
    if eligible:
        best_candidate = sorted(
            eligible,
            key=lambda c: (
                float(c.get("binary_kappa") or -9.0),
                float(c.get("coverage") or 0.0),
                int(c.get("reviewed_pair_count") or 0),
            ),
            reverse=True,
        )[0]

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "parameters": {
            "cluster_id": cluster_id,
            "max_cases": max_cases,
            "max_pairs": max_pairs,
            "base_scoring_profile": base_scoring_profile,
            "snp_strong_threshold_options": strong_thresholds,
            "snp_moderate_threshold_options": moderate_thresholds,
            "temporal_window_options": temporal_windows,
            "strong_epi_weight_options": strong_weights,
            "contradiction_weight_options": contradiction_weights,
            "min_reviewed_pairs": min_reviewed_pairs,
            "max_candidates": max_candidates,
        },
        "candidate_count": len(candidates),
        "best_candidate": best_candidate,
        "top_candidates": sorted(
            [c for c in candidates if c.get("binary_kappa") is not None],
            key=lambda c: (
                float(c.get("binary_kappa") or -9.0),
                float(c.get("coverage") or 0.0),
                int(c.get("reviewed_pair_count") or 0),
            ),
            reverse=True,
        )[:10],
        "notes": [
            "Sweep candidates are ranked by binary Cohen kappa first, then review coverage.",
            "Use this as a calibration aid; retain human review before changing operational thresholds.",
        ],
        **_validation_notice(),
    }


@router.get("/pair-triage-queue")
def pair_triage_queue(
    cluster_id: str | None = Query(None),
    max_cases: int = Query(80, ge=2, le=300),
    max_pairs: int = Query(500, ge=1, le=2000),
    limit: int = Query(100, ge=1, le=500),
    scoring_profile: str = "default_v1",
    snp_strong_threshold: int = Query(5, ge=0, le=100),
    snp_moderate_threshold: int = Query(12, ge=1, le=200),
    temporal_window_days: int = Query(90, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """Operational queue of case pairs ranked by review priority."""
    profile = _resolve_scoring_profile(scoring_profile)
    rows = _case_rows(db)
    if cluster_id:
        cid = _normalise_uuid(cluster_id)
        rows = [r for r in rows if str(r.get("cluster_id") or "") == cid]

    rows.sort(key=lambda r: str(r.get("specimen_date") or "9999-12-31"))
    rows = rows[:max_cases]
    case_ids = {str(r.get("case_id")) for r in rows if r.get("case_id")}
    pair_epi_index = _build_pair_epi_index(db, case_ids)
    payload = _build_case_pair_evidence_payload(
        rows,
        pair_epi_index,
        snp_strong_threshold=snp_strong_threshold,
        snp_moderate_threshold=snp_moderate_threshold,
        temporal_window_days=temporal_window_days,
        max_pairs=max_pairs,
        scoring_profile=profile,
    )

    pair_keys = {_pair_key(str(p["case_a"]), str(p["case_b"])) for p in payload.get("pairs", [])}
    reviews = _latest_pair_review_map(db, pair_keys)

    confidence_points = {
        "strong": 5,
        "moderate": 3,
        "weak": 1,
        "contradicted": -2,
        "insufficient": 0,
        "unknown": 0,
    }

    queue = []
    for pair in payload.get("pairs", []):
        key = _pair_key(str(pair.get("case_a") or ""), str(pair.get("case_b") or ""))
        review = reviews.get(key)
        card = pair.get("evidence_card") or {}
        confidence = str(card.get("confidence") or "unknown")
        temporal = pair.get("temporal_plausibility") or {}
        contradictions = ((pair.get("evidence") or {}).get("contradiction_details") or [])

        priority = confidence_points.get(confidence, 0)
        reasons = [f"confidence:{confidence}"]

        if review:
            priority -= 2
            reasons.append("already_reviewed")
        else:
            priority += 2
            reasons.append("unreviewed")

        if temporal.get("plausible") is True:
            priority += 1
            reasons.append("temporal_plausible")

        major_contradictions = sum(1 for c in contradictions if c.get("severity") == "major")
        if major_contradictions:
            priority -= major_contradictions
            reasons.append(f"major_contradictions:{major_contradictions}")

        completeness = float((pair.get("evidence") or {}).get("data_completeness_score") or 0)
        if completeness >= 0.7:
            priority += 1
            reasons.append("good_data_completeness")

        queue.append(
            {
                "pair": pair.get("pair"),
                "case_a": pair.get("case_a"),
                "case_b": pair.get("case_b"),
                "priority_score": priority,
                "confidence": confidence,
                "overall_interpretation": pair.get("overall_interpretation"),
                "review_status": "reviewed" if review else "unreviewed",
                "review": review,
                "reasons": reasons,
                "data_completeness_score": completeness,
            }
        )

    queue.sort(key=lambda item: (-int(item.get("priority_score") or 0), str(item.get("pair") or "")))

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "parameters": {
            "cluster_id": cluster_id,
            "max_cases": max_cases,
            "max_pairs": max_pairs,
            "limit": limit,
            "scoring_profile": scoring_profile,
            "scoring_profile_version": str(profile.get("version") or "default_v1"),
            "snp_strong_threshold": snp_strong_threshold,
            "snp_moderate_threshold": snp_moderate_threshold,
            "temporal_window_days": temporal_window_days,
        },
        "queue_count": len(queue),
        "queue": queue[:limit],
        **_validation_notice(),
    }


@router.get("/snp-matrix")
def snp_matrix(
    cluster_id: str | None = Query(None),
    max_cases: int = Query(30, ge=5, le=80),
    snp_threshold: int = Query(12, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Return a pairwise SNP distance matrix suitable for a heatmap table."""
    rows = _case_rows(db)
    if cluster_id:
        cid = _normalise_uuid(cluster_id)
        rows = [r for r in rows if str(r["cluster_id"] or "") == cid]

    sequenced = [r for r in rows if r["sequence"]]
    sequenced.sort(key=lambda r: str(r["specimen_date"] or "9999-12-31"))
    sequenced = sequenced[:max_cases]

    case_ids = [str(r["case_id"]) for r in sequenced]
    short_ids = [c[:8] for c in case_ids]
    seq_by_case = {str(r["case_id"]): str(r["sequence"]) for r in sequenced}

    matrix = []
    for left in case_ids:
        row = []
        for right in case_ids:
            row.append(0 if left == right else _snp_distance(seq_by_case[left], seq_by_case[right]))
        matrix.append(row)

    return {
        "cluster_id": cluster_id,
        "case_count": len(case_ids),
        "case_ids": case_ids,
        "short_case_ids": short_ids,
        "matrix": matrix,
        "threshold_hint": snp_threshold,
        "message": f"Distances are pairwise SNP mismatches; <= {snp_threshold} indicates likely linkage under current setting.",
        **_validation_notice(),
    }


@router.get("/advanced-fasta-summary")
def advanced_fasta_summary(db: Session = Depends(get_db)):
    """Summarize advanced FASTA artifacts that are otherwise only raw files."""
    fasta_analysis = _export_json(str(Path(EXPORTS_DIR) / "fasta_analysis_summary.json")) or {}
    seqkit_rows = _parse_tsv(_read_export_text("fasta_analysis/seqkit_stats.tsv"))
    iqtree = _parse_iqtree_summary(_read_export_text("fasta_analysis/iqtree.iqtree"))
    molecular_clock = _parse_molecular_clock(_read_export_text("fasta_analysis/treetime/molecular_clock.txt"))
    snp_agreement = _snp_matrix_agreement(db)

    artifact_counts = {
        "tbprofiler_json": len(list((Path(EXPORTS_DIR) / "tbprofiler" / "results").glob("*.json"))),
        "tbprofiler_vcf": len(list((Path(EXPORTS_DIR) / "tbprofiler" / "vcf").glob("*.vcf.gz"))),
        "mykrobe_json": len(list((Path(EXPORTS_DIR) / "mykrobe").glob("*.json"))),
        "case_action_rows": max(0, len(_read_export_text("appendix_a_case_level_actions.csv").splitlines()) - 1),
        "discordance_review_rows": max(0, len(_read_export_text("appendix_b_full_discordance_review.csv").splitlines()) - 1),
    }

    warnings = list(iqtree.get("warnings") or [])
    clock_r2 = molecular_clock.get("r_squared_value")
    if clock_r2 is not None and clock_r2 < 0.1:
        warnings.append(f"TreeTime root-to-tip temporal signal is weak (r^2={clock_r2:.2f}).")
    if snp_agreement.get("status") == "review":
        warnings.append(f"Internal SNP matrix differs from snp-dists for {snp_agreement.get('mismatch_count')} pair(s).")

    return {
        "status": fasta_analysis.get("status", "not_available"),
        "generated_at": fasta_analysis.get("generated_at"),
        "input": fasta_analysis.get("input", {}),
        "tools": fasta_analysis.get("tools", {}),
        "seqkit_stats": seqkit_rows[0] if seqkit_rows else {},
        "snp_matrix_agreement": snp_agreement,
        "iqtree": iqtree,
        "molecular_clock": molecular_clock,
        "artifact_counts": artifact_counts,
        "warnings": warnings,
        "outputs": fasta_analysis.get("outputs", {}),
        **_validation_notice(),
    }


@router.get("/clusters")
def analytics_clusters(db: Session = Depends(get_db)):
    """Return clusters and basic metadata for analytics selectors."""
    rows = db.execute(text("""
        SELECT cc.cluster_id::text AS cluster_id,
               COUNT(*)::int AS case_count,
               MIN(c.specimen_date) AS first_specimen,
               MAX(c.specimen_date) AS last_specimen
        FROM case_clusters cc
        JOIN cases c ON c.pseudonymised_case_id = cc.sample_id
        GROUP BY cc.cluster_id
        ORDER BY case_count DESC, cc.cluster_id
    """)).mappings().all()

    return {
        "clusters": [
            {
                "cluster_id": str(r["cluster_id"]),
                "cluster_short": str(r["cluster_id"])[:8],
                "case_count": int(r["case_count"] or 0),
                "first_specimen": str(r["first_specimen"]) if r["first_specimen"] else None,
                "last_specimen": str(r["last_specimen"]) if r["last_specimen"] else None,
            }
            for r in rows
        ],
        **_validation_notice(),
    }


@router.get("/resistance-calls")
def resistance_calls(
    sample_id: str | None = Query(None),
    drug: str | None = Query(None),
    prediction: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """Return normalized TB-Profiler resistance calls for dashboards and reports."""
    filters = []
    params: dict[str, object] = {"limit": limit}
    if sample_id:
        filters.append("sample_id = CAST(:sample_id AS uuid)")
        params["sample_id"] = _normalise_uuid(sample_id)
    if drug:
        filters.append("LOWER(drug) = LOWER(:drug)")
        params["drug"] = drug
    if prediction:
        filters.append("LOWER(COALESCE(prediction, '')) = LOWER(:prediction)")
        params["prediction"] = prediction
    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    rows = db.execute(
        text(
            f"""
            SELECT call_id::text AS call_id, sample_id::text AS sample_id, drug,
                   NULLIF(gene, '') AS gene, NULLIF(mutation, '') AS mutation,
                   prediction, confidence, depth, alt_fraction, lineage, source_tool,
                   tool_version, database_version, source_path, created_at
            FROM resistance_calls
            {where}
            ORDER BY created_at DESC NULLS LAST, sample_id, drug, gene, mutation
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()

    calls = [{key: _json_ready(value) for key, value in dict(row).items()} for row in rows]
    resistant = [
        c for c in calls
        if str(c.get("prediction") or "").strip().lower() in {"r", "resistant"}
        or "resistant" in str(c.get("prediction") or "").strip().lower()
    ]
    return {
        "calls": calls,
        "summary": {
            "returned": len(calls),
            "resistant_calls": len(resistant),
            "source_tools": sorted({str(c.get("source_tool") or "") for c in calls if c.get("source_tool")}),
            "database_versions": sorted({str(c.get("database_version") or "") for c in calls if c.get("database_version")}),
        },
        **_validation_notice(),
    }


@router.get("/alerts")
def alerts(
    status: str | None = Query(None),
    severity: str | None = Query(None),
    alert_type: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """Return operational alerts generated from SNP, setting, regional, and resistance rules."""
    filters = []
    params: dict[str, object] = {"limit": limit}
    if status:
        filters.append("LOWER(status) = LOWER(:status)")
        params["status"] = status
    if severity:
        filters.append("LOWER(severity) = LOWER(:severity)")
        params["severity"] = severity
    if alert_type:
        filters.append("LOWER(alert_type) = LOWER(:alert_type)")
        params["alert_type"] = alert_type
    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    rows = db.execute(
        text(
            f"""
            SELECT alert_id::text AS alert_id, alert_type, severity, status,
                   sample_id::text AS sample_id, cluster_id::text AS cluster_id,
                   title, description, evidence, assigned_to, acknowledged_by,
                   acknowledged_at, resolved_by, resolved_at, created_at, updated_at
            FROM alerts
            {where}
            ORDER BY
              CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
              created_at DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    items = [{key: _json_ready(value) for key, value in dict(row).items()} for row in rows]
    return {
        "alerts": items,
        "summary": {
            "returned": len(items),
            "open": sum(1 for item in items if item.get("status") == "open"),
            "critical": sum(1 for item in items if item.get("severity") == "critical"),
            "high": sum(1 for item in items if item.get("severity") == "high"),
        },
        **_validation_notice(),
    }


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(
    alert_id: str,
    payload: AlertStatusUpdate | None = None,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("analyst")),
):
    aid = _normalise_uuid(alert_id)
    result = db.execute(
        text(
            """
            UPDATE alerts
            SET status = 'acknowledged',
                acknowledged_by = :user_id,
                acknowledged_at = NOW(),
                updated_at = NOW()
            WHERE alert_id = CAST(:alert_id AS uuid)
            RETURNING alert_id::text AS alert_id, status
            """
        ),
        {"alert_id": aid, "user_id": user.subject},
    ).mappings().first()
    if not result:
        raise HTTPException(status_code=404, detail="Alert not found")
    _audit(db, action="alert_acknowledged", user_id=user.subject, details={"alert_id": aid, "note": (payload.note if payload else None)})
    db.commit()
    return {"alert_id": aid, "status": result["status"]}


@router.post("/alerts/{alert_id}/assign")
def assign_alert(
    alert_id: str,
    payload: AlertAssignment,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("analyst")),
):
    aid = _normalise_uuid(alert_id)
    result = db.execute(
        text(
            """
            UPDATE alerts
            SET assigned_to = :assigned_to, updated_at = NOW()
            WHERE alert_id = CAST(:alert_id AS uuid)
            RETURNING alert_id::text AS alert_id, assigned_to
            """
        ),
        {"alert_id": aid, "assigned_to": payload.assigned_to},
    ).mappings().first()
    if not result:
        raise HTTPException(status_code=404, detail="Alert not found")
    _audit(db, action="alert_assigned", user_id=user.subject, details={"alert_id": aid, "assigned_to": payload.assigned_to})
    db.commit()
    return {"alert_id": aid, "assigned_to": result["assigned_to"]}


@router.post("/alerts/{alert_id}/resolve")
def resolve_alert(
    alert_id: str,
    payload: AlertStatusUpdate | None = None,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("analyst")),
):
    aid = _normalise_uuid(alert_id)
    result = db.execute(
        text(
            """
            UPDATE alerts
            SET status = 'resolved',
                resolved_by = :user_id,
                resolved_at = NOW(),
                updated_at = NOW()
            WHERE alert_id = CAST(:alert_id AS uuid)
            RETURNING alert_id::text AS alert_id, status
            """
        ),
        {"alert_id": aid, "user_id": user.subject},
    ).mappings().first()
    if not result:
        raise HTTPException(status_code=404, detail="Alert not found")
    _audit(db, action="alert_resolved", user_id=user.subject, details={"alert_id": aid, "note": (payload.note if payload else None)})
    db.commit()
    return {"alert_id": aid, "status": result["status"]}


@router.get("/actions")
def actions(
    status: str | None = Query(None),
    alert_id: str | None = Query(None),
    cluster_id: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    filters = []
    params: dict[str, object] = {"limit": limit}
    if status:
        filters.append("LOWER(status) = LOWER(:status)")
        params["status"] = status
    if alert_id:
        filters.append("alert_id = CAST(:alert_id AS uuid)")
        params["alert_id"] = _normalise_uuid(alert_id)
    if cluster_id:
        filters.append("cluster_id = CAST(:cluster_id AS uuid)")
        params["cluster_id"] = _normalise_uuid(cluster_id)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    rows = db.execute(
        text(
            f"""
            SELECT action_id::text AS action_id, alert_id::text AS alert_id,
                   cluster_id::text AS cluster_id, sample_id::text AS sample_id,
                   action_type, status, owner, note, due_at, completed_at,
                   completed_by, created_at, updated_at
            FROM actions
            {where}
            ORDER BY COALESCE(due_at, created_at) ASC NULLS LAST
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return {"actions": [{key: _json_ready(value) for key, value in dict(row).items()} for row in rows], **_validation_notice()}


@router.post("/actions")
def create_action(
    payload: ActionCreate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("analyst")),
):
    params = {
        "alert_id": _normalise_uuid(payload.alert_id) if payload.alert_id else None,
        "cluster_id": _normalise_uuid(payload.cluster_id) if payload.cluster_id else None,
        "sample_id": _normalise_uuid(payload.sample_id) if payload.sample_id else None,
        "action_type": payload.action_type,
        "owner": payload.owner,
        "note": payload.note,
        "due_at": payload.due_at,
    }
    row = db.execute(
        text(
            """
            INSERT INTO actions (alert_id, cluster_id, sample_id, action_type, owner, note, due_at, created_at, updated_at)
            VALUES (
              CAST(:alert_id AS uuid), CAST(:cluster_id AS uuid), CAST(:sample_id AS uuid),
              :action_type, :owner, :note, :due_at, NOW(), NOW()
            )
            RETURNING action_id::text AS action_id, status
            """
        ),
        params,
    ).mappings().first()
    _audit(db, action="action_created", user_id=user.subject, details={**params, "due_at": str(payload.due_at) if payload.due_at else None})
    db.commit()
    return {"action_id": row["action_id"], "status": row["status"]}


@router.patch("/actions/{action_id}")
def update_action(
    action_id: str,
    payload: ActionUpdate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("analyst")),
):
    aid = _normalise_uuid(action_id)
    completed_at_expr = "NOW()" if payload.status == "completed" else "completed_at"
    row = db.execute(
        text(
            f"""
            UPDATE actions
            SET status = COALESCE(:status, status),
                owner = COALESCE(:owner, owner),
                note = COALESCE(:note, note),
                completed_by = COALESCE(:completed_by, completed_by),
                completed_at = {completed_at_expr},
                updated_at = NOW()
            WHERE action_id = CAST(:action_id AS uuid)
            RETURNING action_id::text AS action_id, status
            """
        ),
        {
            "action_id": aid,
            "status": payload.status,
            "owner": payload.owner,
            "note": payload.note,
            "completed_by": payload.completed_by or (user.subject if payload.status == "completed" else None),
        },
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Action not found")
    _audit(db, action="action_updated", user_id=user.subject, details={"action_id": aid, **payload.model_dump(exclude_none=True)})
    db.commit()
    return {"action_id": aid, "status": row["status"]}


@router.get("/phylo-tree")
def phylo_tree():
    """Return available phylogenetic/transmission visual assets + lightweight graph."""
    net = _export_json(str(EXPORTS_DIR / "transmission_network.json")) or {}
    edges = _load_transmission_edges()
    nodes = net.get("all_nodes") or []

    graph_nodes = []
    for n in nodes:
        full_id = str(n.get("full_case_id") or "")
        if full_id:
            graph_nodes.append({
                "id": full_id,
                "short_id": str(n.get("case_id") or full_id[:8]),
                "risk_band": n.get("risk_band", "low"),
                "risk_score": float(n.get("risk_score") or 0),
            })

    graph_edges = [
        {
            "source": e.get("source"),
            "target": e.get("target"),
            "posterior": _float_or_default(e.get("posterior"), 0.0),
            "confidence": e.get("confidence", "unknown"),
        }
        for e in edges
        if e.get("source") and e.get("target")
    ]

    return {
        "images": {
            "outbreaker_tree": "/cases/outbreaker-image/outbreaker_tree.png",
            "outbreaker_phylo": "/cases/outbreaker-image/outbreaker_phylo.png",
        },
        "graph": {
            "nodes": graph_nodes,
            "edges": graph_edges,
            "node_count": len(graph_nodes),
            "edge_count": len(graph_edges),
        },
        "note": "Use outbreaker tree as primary phylogenetic visual; graph payload supports custom frontend rendering.",
        **_validation_notice(),
    }


@router.get("/timeline")
def timeline(db: Session = Depends(get_db)):
    """Return specimen-date timeline by month plus case-level records."""
    rows = _case_rows(db)
    case_events = []
    by_month = defaultdict(int)

    for r in rows:
        specimen = r["specimen_date"]
        if not specimen:
            continue
        day = str(specimen)
        month = day[:7]
        by_month[month] += 1
        case_events.append({
            "case_id": str(r["case_id"]),
            "short_case_id": str(r["case_id"])[:8],
            "specimen_date": day,
            "month": month,
            "region": str(r["region"]),
            "cluster_id": str(r["cluster_id"] or ""),
            "lineage": str(r["lineage"] or ""),
        })

    monthly_counts = [
        {"month": m, "count": by_month[m]}
        for m in sorted(by_month.keys())
    ]

    case_events.sort(key=lambda x: x["specimen_date"])
    return {
        "monthly_counts": monthly_counts,
        "events": case_events,
        "event_count": len(case_events),
        **_validation_notice(),
    }


_COUNTRY_CENTROID = {
    "Bangladesh": (90.3563, 23.6850),
    "China": (104.1954, 35.8617),
    "United Kingdom": (-3.4360, 55.3781),
    "Indonesia": (113.9213, -0.7893),
    "India": (78.9629, 20.5937),
    "Ireland": (-8.2439, 53.4129),
    "Nigeria": (8.6753, 9.0820),
    "Pakistan": (69.3451, 30.3753),
    "Peru": (-75.0152, -9.1900),
    "Philippines": (121.7740, 12.8797),
    "Ukraine": (31.1656, 48.3794),
    "South Africa": (22.9375, -30.5595),
    "Unknown": (0.0, 0.0),
}


@router.get("/geo-map")
def geo_map(db: Session = Depends(get_db)):
    """Return geography aggregates + centroids for a lightweight map plot."""
    rows = _case_rows(db)

    by_region = defaultdict(lambda: {"case_count": 0, "clusters": set(), "recent_cases_90d": 0})
    today = datetime.utcnow().date()

    for r in rows:
        region = str(r["region"] or "Unknown")
        by_region[region]["case_count"] += 1
        if r["cluster_id"]:
            by_region[region]["clusters"].add(str(r["cluster_id"]))
        specimen = r["specimen_date"]
        if specimen:
            age_days = (today - specimen).days
            if age_days <= 90:
                by_region[region]["recent_cases_90d"] += 1

    points = []
    for region, agg in sorted(by_region.items(), key=lambda kv: (-kv[1]["case_count"], kv[0])):
        lon, lat = _COUNTRY_CENTROID.get(region, (0.0, 0.0))
        points.append({
            "region": region,
            "case_count": agg["case_count"],
            "cluster_count": len(agg["clusters"]),
            "recent_cases_90d": agg["recent_cases_90d"],
            "lon": lon,
            "lat": lat,
        })

    return {"points": points, "point_count": len(points), **_validation_notice()}


@router.get("/cluster-growth")
def cluster_growth(db: Session = Depends(get_db)):
    """Return cumulative growth curves per cluster by specimen month."""
    rows = _case_rows(db)
    by_cluster_month = defaultdict(lambda: defaultdict(int))

    for r in rows:
        cid = str(r["cluster_id"] or "")
        specimen = r["specimen_date"]
        if not cid or not specimen:
            continue
        month = str(specimen)[:7]
        by_cluster_month[cid][month] += 1

    curves = []
    for cid, month_counts in by_cluster_month.items():
        running = 0
        points = []
        for month in sorted(month_counts.keys()):
            running += month_counts[month]
            points.append({"month": month, "new_cases": month_counts[month], "cumulative": running})
        curves.append({
            "cluster_id": cid,
            "cluster_short": cid[:8],
            "points": points,
            "final_size": running,
        })

    curves.sort(key=lambda c: (-c["final_size"], c["cluster_id"]))
    return {"curves": curves, **_validation_notice()}


def _epi_plausible(source_row: dict, target_row: dict, epi_window_days: int) -> bool:
    if not source_row or not target_row:
        return False
    src_date = source_row.get("specimen_date")
    tgt_date = target_row.get("specimen_date")
    if not src_date or not tgt_date:
        return False
    delta = abs((tgt_date - src_date).days)
    same_region = str(source_row.get("region")) == str(target_row.get("region"))
    return delta <= epi_window_days and same_region


@router.get("/genomic-vs-epi")
def genomic_vs_epi(
    snp_threshold: int = Query(12, ge=1, le=100),
    epi_window_days: int = Query(45, ge=1, le=365),
    posterior_min: float = Query(0.0, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
):
    """Compare genomic model links against simple epidemiological plausibility."""
    edges = _load_transmission_edges()

    rows = _case_rows(db)
    case_index = {
        str(r["case_id"]): {
            "case_id": str(r["case_id"]),
            "short_case_id": str(r["case_id"])[:8],
            "specimen_date": r["specimen_date"],
            "region": str(r["region"]),
            "cluster_id": str(r["cluster_id"] or ""),
            "sequence": str(r["sequence"] or ""),
        }
        for r in rows
    }

    compared = []
    for e in edges:
        src = str(e.get("source") or "")
        tgt = str(e.get("target") or "")
        posterior = _float_or_default(e.get("posterior"), 0.0)
        if posterior < posterior_min:
            continue
        if not src or not tgt:
            continue
        src_row = case_index.get(src)
        tgt_row = case_index.get(tgt)
        if not src_row or not tgt_row:
            continue

        snp = None
        if src_row["sequence"] and tgt_row["sequence"]:
            snp = _snp_distance(src_row["sequence"], tgt_row["sequence"])

        genomic_supported = bool(snp is not None and snp <= snp_threshold)
        epi_supported = _epi_plausible(src_row, tgt_row, epi_window_days)
        category = (
            "both_supported" if genomic_supported and epi_supported else
            "genomic_only" if genomic_supported and not epi_supported else
            "epi_only" if epi_supported and not genomic_supported else
            "neither"
        )

        compared.append({
            "source": src,
            "target": tgt,
            "pair": f"{src[:8]} -> {tgt[:8]}",
            "posterior": posterior,
            "confidence": str(e.get("confidence") or "unknown"),
            "posterior_reliability": str(e.get("posterior_reliability") or "unknown"),
            "posterior_reliability_reasons": e.get("posterior_reliability_reasons") or [],
            "snp_distance": snp,
            "genomic_supported": genomic_supported,
            "epi_supported": epi_supported,
            "category": category,
        })

    summary = {
        "total_pairs": len(compared),
        "both_supported": sum(1 for x in compared if x["category"] == "both_supported"),
        "genomic_only": sum(1 for x in compared if x["category"] == "genomic_only"),
        "epi_only": sum(1 for x in compared if x["category"] == "epi_only"),
        "neither": sum(1 for x in compared if x["category"] == "neither"),
    }

    compared.sort(key=lambda x: (-x["posterior"], x["pair"]))
    reliability_reasons = []
    for row in compared:
        for reason in row.get("posterior_reliability_reasons") or []:
            if reason not in reliability_reasons:
                reliability_reasons.append(reason)
    return {
        "parameters": {
            "snp_threshold": snp_threshold,
            "epi_window_days": epi_window_days,
            "posterior_min": posterior_min,
        },
        "summary": summary,
        "pairs": compared[:80],
        "notes": [
            "Epi support here is heuristic: same region and specimen dates within configured window.",
            "Genomic support requires available pairwise sequence distance below configured SNP threshold.",
            *(
                [f"Outbreaker posterior confidence is not assessable: {', '.join(reliability_reasons)}."]
                if any(row.get("confidence") == "not_assessable" for row in compared) and reliability_reasons
                else []
            ),
        ],
        **_validation_notice(),
    }


@router.get("/cluster-dossier/{cluster_id}")
def cluster_dossier(cluster_id: str, db: Session = Depends(get_db)):
    """Build a structured dossier payload for a cluster."""
    cid = _normalise_uuid(cluster_id)

    member_rows = db.execute(text("""
        SELECT c.pseudonymised_case_id::text AS case_id,
               c.specimen_date,
               COALESCE(c.geographic_region, 'Unknown') AS region,
               COALESCE(ti.lineage, '') AS lineage,
               COALESCE(c.case_status, '') AS case_status,
               cs.sequence
        FROM case_clusters cc
        JOIN cases c ON c.pseudonymised_case_id = cc.sample_id
        LEFT JOIN tb_interpretation ti ON ti.sample_id = cc.sample_id
        LEFT JOIN consensus_sequences cs ON cs.sample_id = cc.sample_id
        WHERE cc.cluster_id = CAST(:cid AS UUID)
        ORDER BY c.specimen_date ASC NULLS LAST
    """), {"cid": cid}).mappings().all()

    if not member_rows:
        raise HTTPException(status_code=404, detail="Cluster not found or no members")

    case_ids = [str(r["case_id"]) for r in member_rows]
    regions = sorted({str(r["region"] or "Unknown") for r in member_rows})

    # Quick internal SNP summary.
    seq_rows = [r for r in member_rows if r["sequence"]]
    distances = []
    for i in range(len(seq_rows)):
        for j in range(i + 1, len(seq_rows)):
            distances.append(_snp_distance(str(seq_rows[i]["sequence"]), str(seq_rows[j]["sequence"])))

    dossier = {
        "cluster_id": cid,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "member_count": len(member_rows),
        "regions": regions,
        "period": {
            "first_specimen": str(member_rows[0]["specimen_date"]) if member_rows[0]["specimen_date"] else None,
            "last_specimen": str(member_rows[-1]["specimen_date"]) if member_rows[-1]["specimen_date"] else None,
        },
        "snp_summary": {
            "pair_count": len(distances),
            "min": min(distances) if distances else None,
            "median": sorted(distances)[len(distances) // 2] if distances else None,
            "max": max(distances) if distances else None,
            "pairs_le_12": sum(1 for d in distances if d <= 12),
        },
        "members": [
            {
                "case_id": str(r["case_id"]),
                "short_case_id": str(r["case_id"])[:8],
                "specimen_date": str(r["specimen_date"]) if r["specimen_date"] else None,
                "region": str(r["region"]),
                "lineage": str(r["lineage"] or ""),
                "case_status": str(r["case_status"] or ""),
            }
            for r in member_rows
        ],
        "links": {
            "cluster_investigation_report": f"/cluster-investigations/{cid}/report",
            "outbreak_report": "/cases/outbreak-report.full.html",
        },
        "validation_status": "heuristic_non_validated",
        "warning": (
            "This dossier is heuristic and non-validated. SNP summary and operational interpretations "
            "are for review support only and require calibrated pipelines before real-world use."
        ),
    }
    return dossier


@router.get("/cluster-dossier/{cluster_id}/export")
def export_cluster_dossier(
    cluster_id: str,
    format: str = Query("json", pattern="^(json|html)$"),
    db: Session = Depends(get_db),
):
    dossier = cluster_dossier(cluster_id=cluster_id, db=db)

    if format == "json":
        payload = json.dumps(dossier, indent=2)
        headers = {"Content-Disposition": f"attachment; filename=cluster_dossier_{cluster_id[:8]}.json"}
        return Response(content=payload, media_type="application/json", headers=headers)

    # html
    members_html = "".join(
        f"<tr><td>{m['short_case_id']}</td><td>{m['specimen_date'] or ''}</td><td>{m['region']}</td><td>{m['lineage']}</td><td>{m['case_status']}</td></tr>"
        for m in dossier["members"]
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset=\"utf-8\"><title>Cluster Dossier {dossier['cluster_id'][:8]}</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#111;}}
.card{{border:1px solid #ddd;border-radius:8px;padding:14px 16px;margin-bottom:12px;}}
h1,h2{{margin:0 0 8px 0;}}
table{{width:100%;border-collapse:collapse;font-size:13px;}}
th,td{{border-bottom:1px solid #eee;padding:6px;text-align:left;}}
.small{{color:#666;font-size:12px;}}
.warning{{background:#fff7ed;border:1px solid #fdba74;color:#9a3412;padding:.9rem 1rem;border-radius:8px;margin-bottom:12px;}}
</style></head><body>
<h1>Cluster Dossier</h1>
<p class=\"small\">Generated {dossier['generated_at']}</p>
<div class=\"warning\"><strong>Heuristic / non-validated:</strong> {dossier['warning']}</div>
<div class=\"card\"><h2>Summary</h2>
<p><strong>Cluster:</strong> {dossier['cluster_id']}<br>
<strong>Members:</strong> {dossier['member_count']}<br>
<strong>Regions:</strong> {', '.join(dossier['regions'])}</p></div>
<div class=\"card\"><h2>SNP Summary</h2>
<p>Pairs: {dossier['snp_summary']['pair_count']} | Min: {dossier['snp_summary']['min']} | Median: {dossier['snp_summary']['median']} | Max: {dossier['snp_summary']['max']} | <=12 SNP: {dossier['snp_summary']['pairs_le_12']}</p></div>
<div class=\"card\"><h2>Members</h2>
<table><tr><th>Case</th><th>Specimen date</th><th>Region</th><th>Lineage</th><th>Status</th></tr>{members_html}</table>
</div></body></html>"""
    headers = {"Content-Disposition": f"attachment; filename=cluster_dossier_{cluster_id[:8]}.html"}
    return HTMLResponse(content=html, headers=headers)


@router.get("/transmission-synthesis")
def transmission_synthesis(
    min_posterior: float = Query(0.0, ge=0.0, le=1.0),
    low_snp_threshold: int = Query(12, ge=1, le=100),
    high_snp_contradiction_threshold: int = Query(20, ge=1, le=200),
    temporal_window_days: int = Query(90, ge=1, le=365),
    high_posterior_threshold: float = Query(0.7, ge=0.0, le=1.0),
    rapid_growth_recent_days: int = Query(90, ge=7, le=365),
    rapid_growth_case_threshold: int = Query(4, ge=1, le=100),
    wide_date_spread_days: int = Query(180, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    """Synthesis layer: combine genomic/model/temporal/region evidence into investigation outputs."""
    cfg = SynthesisConfig(
        low_snp_threshold=low_snp_threshold,
        high_snp_contradiction_threshold=high_snp_contradiction_threshold,
        temporal_window_days=temporal_window_days,
        high_posterior_threshold=high_posterior_threshold,
        min_posterior=min_posterior,
        rapid_growth_recent_days=rapid_growth_recent_days,
        rapid_growth_case_threshold=rapid_growth_case_threshold,
        wide_date_spread_days=wide_date_spread_days,
    )
    return build_transmission_synthesis(db=db, cluster_id=None, config=cfg)


@router.get("/transmission-synthesis/{cluster_id}")
def transmission_synthesis_for_cluster(
    cluster_id: str,
    min_posterior: float = Query(0.0, ge=0.0, le=1.0),
    low_snp_threshold: int = Query(12, ge=1, le=100),
    high_snp_contradiction_threshold: int = Query(20, ge=1, le=200),
    temporal_window_days: int = Query(90, ge=1, le=365),
    high_posterior_threshold: float = Query(0.7, ge=0.0, le=1.0),
    rapid_growth_recent_days: int = Query(90, ge=7, le=365),
    rapid_growth_case_threshold: int = Query(4, ge=1, le=100),
    wide_date_spread_days: int = Query(180, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    """Cluster-focused synthesis payload for investigation workflows."""
    cid = _normalise_uuid(cluster_id)
    cfg = SynthesisConfig(
        low_snp_threshold=low_snp_threshold,
        high_snp_contradiction_threshold=high_snp_contradiction_threshold,
        temporal_window_days=temporal_window_days,
        high_posterior_threshold=high_posterior_threshold,
        min_posterior=min_posterior,
        rapid_growth_recent_days=rapid_growth_recent_days,
        rapid_growth_case_threshold=rapid_growth_case_threshold,
        wide_date_spread_days=wide_date_spread_days,
    )
    return build_transmission_synthesis(db=db, cluster_id=cid, config=cfg)


@router.get("/cluster-risk-summary")
def cluster_risk_summary(
    min_posterior: float = Query(0.0, ge=0.0, le=1.0),
    low_snp_threshold: int = Query(12, ge=1, le=100),
    high_snp_contradiction_threshold: int = Query(20, ge=1, le=200),
    temporal_window_days: int = Query(90, ge=1, le=365),
    high_posterior_threshold: float = Query(0.7, ge=0.0, le=1.0),
    rapid_growth_recent_days: int = Query(90, ge=7, le=365),
    rapid_growth_case_threshold: int = Query(4, ge=1, le=100),
    wide_date_spread_days: int = Query(180, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    """Operational cluster ranking derived from synthesis outputs."""
    cfg = SynthesisConfig(
        low_snp_threshold=low_snp_threshold,
        high_snp_contradiction_threshold=high_snp_contradiction_threshold,
        temporal_window_days=temporal_window_days,
        high_posterior_threshold=high_posterior_threshold,
        min_posterior=min_posterior,
        rapid_growth_recent_days=rapid_growth_recent_days,
        rapid_growth_case_threshold=rapid_growth_case_threshold,
        wide_date_spread_days=wide_date_spread_days,
    )
    return build_cluster_risk_summary(db=db, config=cfg)

