from __future__ import annotations

import json
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.synthesis.epi_evidence import compute_epi_evidence, load_epi_records_for_cases
from backend.synthesis.explanations import category_display, interpretation_text, recommended_actions
from backend.synthesis.flags import cluster_flags, pair_flags
from backend.synthesis.scoring import (
    cluster_priority_score,
    confidence_category,
    pair_priority_score,
    score_band,
    serialise_parameters,
)


@dataclass(frozen=True)
class SynthesisConfig:
    low_snp_threshold: int = 12
    high_snp_contradiction_threshold: int = 20
    temporal_window_days: int = 45
    high_posterior_threshold: float = 0.7
    min_posterior: float = 0.0
    rapid_growth_recent_days: int = 90
    rapid_growth_case_threshold: int = 4
    wide_date_spread_days: int = 180


def _export_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _export_csv_map(path: str) -> dict[str, dict[str, str]]:
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows: dict[str, dict[str, str]] = {}
            for row in reader:
                case_id = str(row.get("case_id") or "").strip()
                if case_id:
                    rows[case_id] = row
            return rows
    except Exception:
        return {}


def _load_sequence_proxy() -> tuple[dict[str, dict[str, str]], int, float]:
    assignments = _export_csv_map("exports/sequence_cluster_assignments.csv")
    summary = _export_json("exports/sequence_clustering_summary.json")
    comparison = _export_json("exports/cluster_method_comparison.json")
    threshold = int(summary.get("threshold_snp_distance") or 25)
    agreement = comparison.get("agreement") or {}
    precision = float(agreement.get("pairwise_precision_outbreaker_vs_sequence") or 1.0)
    return assignments, threshold, precision


def _sequence_proxy_distance(
    source_id: str,
    target_id: str,
    assignments: dict[str, dict[str, str]],
    threshold: int,
) -> tuple[int | None, str, bool | None]:
    source_row = assignments.get(source_id)
    target_row = assignments.get(target_id)
    if not source_row or not target_row:
        return None, "sequence_cluster_assignment_unavailable", None

    source_cluster = str(source_row.get("cluster_id") or "")
    target_cluster = str(target_row.get("cluster_id") or "")
    if not source_cluster or not target_cluster:
        return None, "sequence_cluster_assignment_unavailable", None

    same_cluster = source_cluster == target_cluster
    return (0 if same_cluster else threshold + 1), "sequence_cluster_proxy", same_cluster


def _is_resistant(value: Any) -> bool:
    if isinstance(value, dict):
        for status in value.values():
            if str(status).strip().lower() in {"r", "resistant"}:
                return True
        return False
    if isinstance(value, list):
        return len(value) > 0
    if isinstance(value, str):
        return str(value).strip().lower() in {"r", "resistant", "mdr", "xdr"}
    return False


def _case_rows(db: Session, cluster_id: str | None = None):
    sql = """
        SELECT c.pseudonymised_case_id::text AS case_id,
               c.specimen_date,
               COALESCE(c.geographic_region, 'Unknown') AS region,
               COALESCE(cc.cluster_id::text, '') AS cluster_id,
               COALESCE(ti.lineage, '') AS lineage,
               ti.predicted_drug_resistance,
               cs.sequence,
               LOWER(COALESCE(sqm.qc_status, 'not_reported')) AS qc_status,
               COALESCE(sqm.contamination_flag, FALSE) AS contamination_flag
        FROM cases c
        LEFT JOIN case_clusters cc ON cc.sample_id = c.pseudonymised_case_id
        LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
        LEFT JOIN consensus_sequences cs ON cs.sample_id = c.pseudonymised_case_id
        LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id = c.pseudonymised_case_id
    """
    params: dict[str, Any] = {}
    if cluster_id:
        sql += " WHERE cc.cluster_id = CAST(:cluster_id AS UUID)"
        params["cluster_id"] = cluster_id

    return db.execute(text(sql), params).mappings().all()


def _normalise_cluster_id(cluster_id: str) -> str:
    val = (cluster_id or "").strip()
    if len(val) != 36 or val.count("-") != 4:
        raise ValueError("cluster_id must be a UUID")
    return val


def _bool_temporal_support(source_date, target_date, window_days: int) -> bool:
    if not source_date or not target_date:
        return False
    return abs((target_date - source_date).days) <= window_days


def _epi_support_level(temporal_support: bool, geographic_support: bool) -> str:
    if temporal_support and geographic_support:
        return "temporal_and_geographic"
    if temporal_support:
        return "temporal_only"
    if geographic_support:
        return "geographic_only"
    return "none"


def _validation_notice() -> dict[str, str]:
    return {
        "validation_status": "heuristic_non_validated",
        "warning": (
            "This synthesis output is heuristic and non-validated. Sequence support is derived from "
            "precomputed sequence-cluster assignments rather than a raw SNP pipeline, and scores require "
            "calibration before real-world use. Epidemiological support uses structured database records "
            "where available, falling back to temporal and geographic proximity when records are absent."
        ),
    }


def build_transmission_synthesis(
    db: Session,
    *,
    cluster_id: str | None = None,
    config: SynthesisConfig | None = None,
) -> dict[str, Any]:
    cfg = config or SynthesisConfig()
    if cluster_id:
        cluster_id = _normalise_cluster_id(cluster_id)

    case_rows = _case_rows(db, cluster_id=cluster_id)
    sequence_assignments, sequence_threshold, sequence_precision = _load_sequence_proxy()
    case_index: dict[str, dict[str, Any]] = {}
    for row in case_rows:
        cid = str(row["case_id"])
        case_index[cid] = {
            "case_id": cid,
            "short_case_id": cid[:8],
            "specimen_date": row["specimen_date"],
            "region": str(row["region"]),
            "cluster_id": str(row["cluster_id"] or ""),
            "lineage": str(row["lineage"] or ""),
            "predicted_drug_resistance": row["predicted_drug_resistance"],
            "sequence": str(row["sequence"] or ""),
            "qc_status": str(row["qc_status"] or "not_reported"),
            "contamination_flag": bool(row["contamination_flag"]),
            "sequence_cluster_id": str((sequence_assignments.get(cid) or {}).get("cluster_id") or ""),
        }

    if not case_index:
        return {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "cluster_id": cluster_id,
            "summary": {"cluster_count": 0, "pair_count": 0},
            "clusters": [],
            "pairs": [],
            "parameters": serialise_parameters(cfg.__dict__),
            "calibration": {
                "sequence_cluster_threshold_snp_distance": sequence_threshold,
                "outbreaker_vs_sequence_pairwise_precision": sequence_precision,
            },
            **_validation_notice(),
        }

    net = _export_json("exports/transmission_network.json")
    edges = net.get("edges") or []

    # Bulk-load structured epi records for all cases so per-pair queries are avoided.
    epi_records = load_epi_records_for_cases(db, case_ids=list(case_index.keys()))

    cluster_members: dict[str, set[str]] = {}
    for case in case_index.values():
        ckey = case["cluster_id"] or "unclustered"
        cluster_members.setdefault(ckey, set()).add(case["case_id"])

    pairs: list[dict[str, Any]] = []
    cluster_pair_counts: dict[str, int] = {}
    cluster_strong_or_contradictory: dict[str, int] = {}

    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        posterior = float(edge.get("probability") or 0.0)
        edge_conf = str(edge.get("confidence") or "unknown")

        if posterior < cfg.min_posterior or not source or not target:
            continue
        if source not in case_index or target not in case_index:
            continue

        src = case_index[source]
        tgt = case_index[target]

        # Keep synthesis cluster-centric: pair belongs to source cluster unless mismatched.
        pair_cluster = src["cluster_id"] or tgt["cluster_id"] or "unclustered"
        if src["cluster_id"] and tgt["cluster_id"] and src["cluster_id"] != tgt["cluster_id"]:
            pair_cluster = "cross_cluster"

        source_has_seq = bool(src["sequence"])
        target_has_seq = bool(tgt["sequence"])
        snp, snp_source, sequence_cluster_match = _sequence_proxy_distance(
            source,
            target,
            sequence_assignments,
            sequence_threshold,
        )

        temporal_support = _bool_temporal_support(src["specimen_date"], tgt["specimen_date"], cfg.temporal_window_days)
        geographic_support = src["region"] == tgt["region"]

        # Structured epi evidence from database records (replaces proxy-only approach)
        epi_ev = compute_epi_evidence(
            source,
            target,
            epi_records,
            specimen_date_a=src["specimen_date"],
            specimen_date_b=tgt["specimen_date"],
            temporal_window_days=cfg.temporal_window_days,
        )
        epi_support = epi_ev["epi_support_level"]

        # Retain legacy proxy level as fallback label when DB has no records.
        # When both cases lack structured epi records, fall back entirely to
        # the specimen-date + geographic-region proxy.  When structured records
        # yielded temporal_only but the cases share a region, upgrade the label.
        no_records_both = (
            "no_epi_records_case_a" in epi_ev.get("missing_data", [])
            and "no_epi_records_case_b" in epi_ev.get("missing_data", [])
        )
        if no_records_both and (temporal_support or geographic_support):
            epi_support = _epi_support_level(temporal_support, geographic_support)
        elif epi_support == "temporal_only" and geographic_support:
            epi_support = "temporal_and_geographic"

        src_qc_ok = src["qc_status"] in {"pass", "passed"} and not src["contamination_flag"]
        tgt_qc_ok = tgt["qc_status"] in {"pass", "passed"} and not tgt["contamination_flag"]

        p_flags = pair_flags(
            snp_distance=snp,
            posterior_probability=posterior,
            geographic_support=geographic_support,
            edge_confidence=edge_conf,
            source_has_sequence=source_has_seq,
            target_has_sequence=target_has_seq,
            source_qc_ok=src_qc_ok,
            target_qc_ok=tgt_qc_ok,
            low_snp_threshold=cfg.low_snp_threshold,
            high_snp_contradiction_threshold=cfg.high_snp_contradiction_threshold,
            high_posterior_threshold=cfg.high_posterior_threshold,
        )

        category = confidence_category(
            snp_distance=snp,
            posterior_probability=posterior,
            epi_support_level=epi_support,
            low_snp_threshold=cfg.low_snp_threshold,
            high_snp_contradiction_threshold=cfg.high_snp_contradiction_threshold,
            high_posterior_threshold=cfg.high_posterior_threshold,
        )

        interpretation = interpretation_text(category, p_flags)
        priority = pair_priority_score(
            category=category,
            posterior_probability=posterior,
            snp_distance=snp,
            pair_flags=p_flags,
            epi_support_level=epi_support,
        )
        actions = recommended_actions(category, p_flags)

        pairs.append(
            {
                "cluster_id": pair_cluster,
                "source": source,
                "target": target,
                "snp_distance": snp,
                "posterior_probability": round(posterior, 4),
                "temporal_support": temporal_support,
                "geographic_support": geographic_support,
                "epi_support": epi_support,
                "sequence_cluster_match": sequence_cluster_match,
                "snp_distance_source": snp_source,
                "confidence": category_display(category),
                "confidence_code": category,
                "priority_score": priority,
                "priority_band": score_band(priority),
                "interpretation": interpretation,
                "flags": p_flags,
                "recommended_review_actions": actions,
                "epi_evidence": epi_ev,
                "validation_status": "heuristic_non_validated",
            }
        )

        cluster_pair_counts[pair_cluster] = cluster_pair_counts.get(pair_cluster, 0) + 1
        if category in {"strong_support", "contradictory"}:
            cluster_strong_or_contradictory[pair_cluster] = cluster_strong_or_contradictory.get(pair_cluster, 0) + 1

    by_cluster: list[dict[str, Any]] = []
    recent_cutoff = datetime.utcnow().date() - timedelta(days=cfg.rapid_growth_recent_days)

    for cluster_key, members in sorted(cluster_members.items()):
        if cluster_id and cluster_key != cluster_id:
            continue

        member_rows = [case_index[m] for m in members if m in case_index]
        specimen_dates = [m["specimen_date"] for m in member_rows if m["specimen_date"]]
        regions = {m["region"] for m in member_rows if m["region"]}
        resistance_count = sum(1 for m in member_rows if _is_resistant(m["predicted_drug_resistance"]))
        missing_sequence_or_qc = sum(
            1
            for m in member_rows
            if (not m["sequence"]) or (m["qc_status"] not in {"pass", "passed"}) or m["contamination_flag"]
        )
        recent_case_count = sum(
            1 for m in member_rows if m["specimen_date"] and m["specimen_date"] >= recent_cutoff
        )

        c_flags = cluster_flags(
            cluster_regions=regions,
            specimen_dates=specimen_dates,
            resistance_case_count=resistance_count,
            missing_sequence_or_qc_case_count=missing_sequence_or_qc,
            recent_case_count=recent_case_count,
            wide_date_spread_days=cfg.wide_date_spread_days,
            rapid_growth_case_threshold=cfg.rapid_growth_case_threshold,
        )

        c_pair_count = cluster_pair_counts.get(cluster_key, 0)
        c_strong_or_contradictory = cluster_strong_or_contradictory.get(cluster_key, 0)

        c_priority = cluster_priority_score(
            member_count=len(member_rows),
            pair_count=c_pair_count,
            strong_or_contradictory_pairs=c_strong_or_contradictory,
            cluster_flags=c_flags,
            evidence_scale=sequence_precision,
        )

        cluster_pairs = [p for p in pairs if p["cluster_id"] == cluster_key]
        cluster_pairs.sort(key=lambda x: (-x["priority_score"], -x["posterior_probability"]))

        c_actions = recommended_actions("moderate_support", c_flags)
        first_specimen = min(specimen_dates).isoformat() if specimen_dates else None
        last_specimen = max(specimen_dates).isoformat() if specimen_dates else None

        by_cluster.append(
            {
                "cluster_id": cluster_key,
                "cluster_short": cluster_key[:8],
                "summary": {
                    "member_count": len(member_rows),
                    "pair_count": c_pair_count,
                    "regions": sorted(regions),
                    "first_specimen": first_specimen,
                    "last_specimen": last_specimen,
                    "resistance_case_count": resistance_count,
                    "missing_sequence_or_qc_case_count": missing_sequence_or_qc,
                    "recent_case_count": recent_case_count,
                    "priority_score": c_priority,
                    "priority_band": score_band(c_priority),
                },
                "confidence_counts": {
                    "strong_support": sum(1 for p in cluster_pairs if p["confidence_code"] == "strong_support"),
                    "moderate_support": sum(1 for p in cluster_pairs if p["confidence_code"] == "moderate_support"),
                    "genomic_only_signal": sum(1 for p in cluster_pairs if p["confidence_code"] == "genomic_only_signal"),
                    "model_only_signal": sum(1 for p in cluster_pairs if p["confidence_code"] == "model_only_signal"),
                    "contradictory": sum(1 for p in cluster_pairs if p["confidence_code"] == "contradictory"),
                    "insufficient_evidence": sum(1 for p in cluster_pairs if p["confidence_code"] == "insufficient_evidence"),
                },
                "flags": c_flags,
                "recommended_investigation_actions": c_actions,
                "explanation": (
                    "Synthesis integrates SNP distance, Outbreaker posterior, and structured epidemiological evidence "
                    "(shared contacts, locations, and exposures from database records) into investigation-ready "
                    "confidence categories and review priorities."
                ),
                "pairwise_transmission_evidence": cluster_pairs,
                "validation_status": "heuristic_non_validated",
            }
        )

    by_cluster.sort(key=lambda x: (-x["summary"]["priority_score"], x["cluster_id"]))
    pairs.sort(key=lambda x: (-x["priority_score"], -x["posterior_probability"]))

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "cluster_id": cluster_id,
        "summary": {
            "cluster_count": len(by_cluster),
            "pair_count": len(pairs),
            "high_priority_pairs": sum(1 for p in pairs if p["priority_score"] >= 70),
            "contradictory_pairs": sum(1 for p in pairs if p["confidence_code"] == "contradictory"),
        },
        "clusters": by_cluster,
        "pairs": pairs,
        "parameters": serialise_parameters(cfg.__dict__),
        "calibration": {
            "sequence_cluster_threshold_snp_distance": sequence_threshold,
            "outbreaker_vs_sequence_pairwise_precision": sequence_precision,
        },
        **_validation_notice(),
    }


def build_cluster_risk_summary(db: Session, *, config: SynthesisConfig | None = None) -> dict[str, Any]:
    payload = build_transmission_synthesis(db, cluster_id=None, config=config)
    items = []
    for cluster in payload.get("clusters", []):
        summary = cluster.get("summary", {})
        items.append(
            {
                "cluster_id": cluster.get("cluster_id"),
                "cluster_short": cluster.get("cluster_short"),
                "member_count": summary.get("member_count", 0),
                "pair_count": summary.get("pair_count", 0),
                "priority_score": summary.get("priority_score", 0),
                "priority_band": summary.get("priority_band", "low"),
                "flags": cluster.get("flags", []),
                "top_recommended_actions": cluster.get("recommended_investigation_actions", [])[:3],
            }
        )

    items.sort(key=lambda x: (-int(x.get("priority_score", 0)), str(x.get("cluster_id") or "")))
    return {
        "generated_at": payload.get("generated_at"),
        "summary": payload.get("summary", {}),
        "clusters": items,
        "parameters": payload.get("parameters", {}),
        "validation_status": payload.get("validation_status", "heuristic_non_validated"),
        "warning": payload.get(
            "warning",
            "This synthesis output is heuristic and non-validated. Scores require calibration before use.",
        ),
    }

