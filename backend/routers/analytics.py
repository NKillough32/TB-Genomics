import json
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, StringConstraints
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.auth import AuthenticatedUser, require_roles
from backend.database import SessionLocal
from backend.snp_validation import validated_snp_distance
from backend.synthesis.transmission_synthesis import (
    SynthesisConfig,
    build_cluster_risk_summary,
    build_transmission_synthesis,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])

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


class CasePairReviewUpsert(BaseModel):
    case_a: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=36)]
    case_b: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=36)]
    reviewer_classification: ReviewClassification
    notes: str | None = None
    reviewer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None
    cluster_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=36)] | None = None


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _export_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


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
               cs.sequence
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

    network = _export_json("exports/transmission_network.json") or {}
    edges = network.get("edges") or []
    cluster_set = set(case_ids)
    high_conf_edges = 0
    for edge in edges:
        src = str(edge.get("source") or "")
        tgt = str(edge.get("target") or "")
        if src in cluster_set and tgt in cluster_set and float(edge.get("probability") or 0.0) >= 0.70:
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
    """)).mappings().all()

    contact_rows = db.execute(text("""
        SELECT case_id::text AS case_id,
               contact_id::text AS contact_id
        FROM case_contact_links
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
            "by_label": {},
            "confusion": {},
        }

    supportive = {"confirmed transmission", "probable transmission", "possible transmission"}
    exact_matches = 0
    binary_matches = 0
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
        if model_supportive == reviewer_supportive:
            binary_matches += 1

        by_label[reviewer_label]["count"] += 1
        if exact:
            by_label[reviewer_label]["exact_matches"] += 1
        confusion[reviewer_label][model_label] += 1

    return {
        "reviewed_pairs": reviewed,
        "exact_agreement": round(exact_matches / reviewed, 4),
        "binary_agreement": round(binary_matches / reviewed, 4),
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


def _build_case_pair_evidence_payload(
    rows: list,
    pair_epi_index: dict[tuple[str, str], dict],
    *,
    snp_strong_threshold: int,
    snp_moderate_threshold: int,
    temporal_window_days: int,
    max_pairs: int,
) -> dict:
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
        score_map = {
            "strong epi support": 3,
            "moderate epi support": 2,
            "weak epi support": 1,
            "contradictory": -3,
            "unknown": 0,
        }
        overall_score += score_map.get(genomic_support, 0)
        overall_score += score_map.get(epi_support, 0)
        if resistance_concordance == "concordant":
            overall_score += 1
        elif resistance_concordance == "discordant":
            overall_score -= 1

        if overall_score >= 5:
            overall_interpretation = "strong support, needs reviewer confirmation"
        elif overall_score >= 3:
            overall_interpretation = "moderate support, needs reviewer confirmation"
        elif overall_score >= 1:
            overall_interpretation = "weak support, review with caution"
        elif contradictory_evidence:
            overall_interpretation = "contradictory evidence"
        else:
            overall_interpretation = "insufficient evidence"

        support_summary[overall_interpretation] += 1

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
                    "basis": evidence_basis,
                    "observation_mode": observation_mode,
                },
                "overall_interpretation": overall_interpretation,
                "reviewer_classification": None,
            }
        )

    return {
        "pair_count": len(pairs),
        "support_summary": dict(support_summary),
        "pairs": pairs,
        "reviewer_classification_options": list(REVIEW_CLASSIFICATIONS),
    }


@router.get("/case-pair-evidence")
def case_pair_evidence(
    cluster_id: str | None = Query(None),
    max_cases: int = Query(40, ge=2, le=120),
    max_pairs: int = Query(250, ge=1, le=2000),
    snp_strong_threshold: int = Query(5, ge=0, le=100),
    snp_moderate_threshold: int = Query(12, ge=1, le=200),
    temporal_window_days: int = Query(45, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """Build structured transmission-link evidence for case pairs."""
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
            reviewed_at
        )
        VALUES (
            CAST(:case_a AS UUID),
            CAST(:case_b AS UUID),
            :classification,
            :reviewer,
            :notes,
            CAST(:cluster_id AS UUID),
            NOW()
        )
        ON CONFLICT (case_a, case_b)
        DO UPDATE SET
            reviewer_classification = EXCLUDED.reviewer_classification,
            reviewer = EXCLUDED.reviewer,
            notes = EXCLUDED.notes,
            source_cluster_id = EXCLUDED.source_cluster_id,
            reviewed_at = NOW()
    """), {
        "case_a": case_a,
        "case_b": case_b,
        "classification": payload.reviewer_classification,
        "reviewer": reviewer,
        "notes": payload.notes,
        "cluster_id": cluster_id,
    })
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

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT case_a::text AS case_a,
               case_b::text AS case_b,
               reviewer_classification,
               reviewer,
               notes,
               source_cluster_id::text AS cluster_id,
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
                "reviewed_at": r.get("reviewed_at").isoformat() if r.get("reviewed_at") else None,
            }
            for r in rows
        ],
        "reviewer_classification_options": list(REVIEW_CLASSIFICATIONS),
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


@router.get("/case-pair-calibration")
def case_pair_calibration(
    cluster_id: str | None = Query(None),
    max_cases: int = Query(80, ge=2, le=300),
    max_pairs: int = Query(1000, ge=1, le=10000),
    snp_strong_threshold: int = Query(5, ge=0, le=100),
    snp_moderate_threshold: int = Query(12, ge=1, le=200),
    temporal_window_days: int = Query(45, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """Compare model pair interpretations with reviewer classifications."""
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

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "parameters": {
            "cluster_id": cluster_id,
            "max_cases": max_cases,
            "max_pairs": max_pairs,
            "snp_strong_threshold": snp_strong_threshold,
            "snp_moderate_threshold": snp_moderate_threshold,
            "temporal_window_days": temporal_window_days,
        },
        "model_pair_count": len(pairs),
        "reviewed_pair_count": len(comparisons),
        "coverage": round(len(comparisons) / len(pairs), 4) if pairs else None,
        "summary": summary,
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


@router.get("/phylo-tree")
def phylo_tree():
    """Return available phylogenetic/transmission visual assets + lightweight graph."""
    net = _export_json("exports/transmission_network.json") or {}
    edges = net.get("edges") or []
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
            "posterior": float(e.get("probability") or 0),
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
    net = _export_json("exports/transmission_network.json") or {}
    edges = net.get("edges") or []

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
        posterior = float(e.get("probability") or 0)
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
    temporal_window_days: int = Query(45, ge=1, le=365),
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
    temporal_window_days: int = Query(45, ge=1, le=365),
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
    temporal_window_days: int = Query(45, ge=1, le=365),
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
