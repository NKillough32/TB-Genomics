from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from itertools import combinations
from statistics import median
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.routers.case_overview import surveillance_kpis
from backend.routers.cases import (
    _confidence_tier,
    _load_export_json,
    _pair_key,
    _pairwise_matrix,
    _resistance_profile_text,
    _short_case_id,
)
from backend.synthesis.transmission_synthesis import _major_lineage

JsonDict = dict[str, Any]
RowDict = dict[str, Any]


@dataclass(slots=True)
class OutbreakReportData:
    total_cases: int
    clustered_cases: int
    open_clusters: int
    summary_data: JsonDict | None
    transmission_data: JsonDict | None
    decycled_consensus_data: JsonDict | None
    synthesis_data: JsonDict
    lineage_dr_data: JsonDict | None
    resistance_validation_data: JsonDict | None
    secondary_validation_data: JsonDict | None
    method_comparison_data: JsonDict | None
    sequence_summary_data: JsonDict | None
    fasta_analysis_data: JsonDict | None
    synthesis_pairs: list[JsonDict]
    synthesis_parameters: JsonDict
    low_snp_threshold: int
    high_snp_contradiction_threshold: int
    high_posterior_threshold: float
    high_posterior_label: str
    summary_provenance: str
    synthesis_format_version: Any
    kpi_data: JsonDict | None
    qc_table_exists: bool
    weekly_trends: list[RowDict]
    case_rows: list[RowDict]
    case_by_id: dict[str, RowDict]
    sequence_by_case: dict[str, str]
    pairwise_snp_matrix: dict[tuple[str, str], int]
    transmission_edges: list[JsonDict]
    enriched_network_edges: list[JsonDict]
    enriched_high_confidence_edges: list[JsonDict]
    best_incoming: dict[str, RowDict]
    best_outgoing: dict[str, RowDict]
    outbreaker_pair_prob: dict[tuple[str, str], float]
    high_confidence_edges: list[JsonDict]
    high_confidence_all_count: int
    high_confidence_snapshot_count: int
    sequence_cluster_members: dict[str, list[str]]
    sequence_pair_set: set[tuple[str, str]]
    nearest_neighbor_snp: dict[str, int]
    cluster_pairwise_stats: dict[str, RowDict]
    qc_status_counts: dict[str, int]
    excluded_from_outbreaker: int
    cluster_action_rows: list[RowDict]
    cluster_epi_rows: list[RowDict]
    genomic_pairs: list[RowDict]
    model_only_pairs: list[RowDict]
    genomically_discordant: list[RowDict]
    qc_resolution_pairs: list[RowDict]
    discordant_pairs: list[RowDict]


def build_outbreak_report_data(db: Session) -> OutbreakReportData:
    total_cases = _run_scalar_query(db, "SELECT COUNT(*) FROM cases")
    clustered_cases = _run_scalar_query(db, "SELECT COUNT(DISTINCT sample_id) FROM case_clusters")
    open_clusters = _run_scalar_query(
        db,
        "SELECT COUNT(*) FROM clusters WHERE investigation_status = 'open'",
    )

    summary_data = _load_export_json("outbreaker_summary.json")
    transmission_data = _load_export_json("transmission_network.json")
    decycled_consensus_data = _load_export_json("outbreaker_decycled_consensus.json")
    synthesis_data = _load_export_json("synthesis_output.json") or {}
    lineage_dr_data = _load_export_json("lineage_dr_validation.json")
    resistance_validation_data = _load_export_json("resistance_validation.json")
    secondary_validation_data = _load_export_json("secondary_engine_validation.json")
    method_comparison_data = _load_export_json("cluster_method_comparison.json")
    sequence_summary_data = _load_export_json("sequence_clustering_summary.json")
    fasta_analysis_data = _load_export_json("fasta_analysis_summary.json")

    synthesis_pairs = synthesis_data.get("pairs") if isinstance(synthesis_data, dict) else []
    if not isinstance(synthesis_pairs, list):
        synthesis_pairs = []
    synthesis_clusters = synthesis_data.get("clusters") if isinstance(synthesis_data, dict) else []
    if not isinstance(synthesis_clusters, list):
        synthesis_clusters = []
    synthesis_parameters = synthesis_data.get("parameters") if isinstance(synthesis_data, dict) else {}
    if not isinstance(synthesis_parameters, dict):
        synthesis_parameters = {}

    low_snp_threshold = int(synthesis_parameters.get("low_snp_threshold") or 12)
    high_snp_contradiction_threshold = int(
        synthesis_parameters.get("high_snp_contradiction_threshold") or 20
    )
    high_posterior_threshold = float(synthesis_parameters.get("high_posterior_threshold") or 0.70)
    high_posterior_label = f"{high_posterior_threshold:.2f}"
    summary_provenance = (
        str(summary_data.get("data_provenance") or "unknown")
        if isinstance(summary_data, dict)
        else "unknown"
    )
    synthesis_format_version = (
        synthesis_data.get("format_version") if isinstance(synthesis_data, dict) else None
    )

    synthesis_pair_by_directed: dict[tuple[str, str], JsonDict] = {}
    synthesis_pair_by_unordered: dict[tuple[str, str], JsonDict] = {}
    for pair in synthesis_pairs:
        if not isinstance(pair, dict):
            continue
        source = str(pair.get("source") or "")
        target = str(pair.get("target") or "")
        if not source or not target:
            continue
        synthesis_pair_by_directed[(source, target)] = pair
        synthesis_pair_by_unordered[tuple(sorted([source, target]))] = pair

    synthesis_cluster_by_id = {
        str(cluster.get("cluster_id")): cluster
        for cluster in synthesis_clusters
        if isinstance(cluster, dict) and cluster.get("cluster_id")
    }

    try:
        kpi_data = surveillance_kpis(weeks=12, db=db)
    except Exception:
        kpi_data = None
        db.rollback()

    qc_table_exists = bool(
        _run_scalar_query(db, "SELECT to_regclass('public.sample_qc_metrics') IS NOT NULL")
    )
    weekly_trends = _weekly_trends(db, qc_table_exists)
    case_rows = _case_rows(db)
    case_by_id = {str(row.get("case_id")): row for row in case_rows if row.get("case_id")}
    sequence_by_case = {
        str(row.get("case_id")): str(row.get("sequence") or "").strip().upper()
        for row in case_rows
        if row.get("case_id") and row.get("sequence")
    }
    pairwise_snp_matrix = _pairwise_matrix(sequence_by_case)

    transmission_edges = _transmission_edges(transmission_data)
    enriched_network_edges = _enrich_network_edges(transmission_edges)
    enriched_high_confidence_edges = [
        edge
        for edge in enriched_network_edges
        if float(edge.get("probability") or 0.0) >= high_posterior_threshold
    ]

    best_incoming: dict[str, RowDict] = {}
    best_outgoing: dict[str, RowDict] = {}
    outbreaker_pair_prob: dict[tuple[str, str], float] = {}
    for edge in transmission_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if not source or not target:
            continue
        probability = float(edge.get("probability") or 0.0)
        if target not in best_incoming or probability > best_incoming[target]["probability"]:
            best_incoming[target] = {"source": source, "probability": probability}
        if source not in best_outgoing or probability > best_outgoing[source]["probability"]:
            best_outgoing[source] = {"target": target, "probability": probability}
        pair_key = tuple(sorted([source, target]))
        if pair_key not in outbreaker_pair_prob or probability > outbreaker_pair_prob[pair_key]:
            outbreaker_pair_prob[pair_key] = probability

    sequence_cluster_members: dict[str, list[str]] = {}
    for row in case_rows:
        cluster_id = str(row.get("cluster_id") or "")
        case_id = str(row.get("case_id") or "")
        if cluster_id and case_id:
            sequence_cluster_members.setdefault(cluster_id, []).append(case_id)

    sequence_pair_set: set[tuple[str, str]] = set()
    nearest_neighbor_snp: dict[str, int] = {}
    for pair_key, distance in pairwise_snp_matrix.items():
        if distance <= low_snp_threshold:
            sequence_pair_set.add(pair_key)

    for case_id in sequence_by_case:
        relevant = []
        for other_case_id in sequence_by_case:
            if other_case_id == case_id:
                continue
            distance = pairwise_snp_matrix.get(_pair_key(case_id, other_case_id))
            if distance is not None:
                relevant.append((distance, other_case_id))
        if relevant:
            best_distance, _best_partner = sorted(relevant, key=lambda item: (item[0], item[1]))[0]
            nearest_neighbor_snp[case_id] = int(best_distance)

    cluster_pairwise_stats: dict[str, RowDict] = {}
    for cluster_id, members in sequence_cluster_members.items():
        unique_members = sorted(set(members))
        sequenced_members = [case_id for case_id in unique_members if sequence_by_case.get(case_id)]
        distances = [
            pairwise_snp_matrix[_pair_key(left, right)]
            for left, right in combinations(sequenced_members, 2)
            if _pair_key(left, right) in pairwise_snp_matrix
        ]
        cluster_pairwise_stats[cluster_id] = {
            "member_count": len(unique_members),
            "sequenced_member_count": len(sequenced_members),
            "pairwise_min": min(distances) if distances else None,
            "pairwise_median": median(distances) if distances else None,
            "pairwise_max": max(distances) if distances else None,
            "links_le_5": sum(1 for value in distances if value <= 5),
            "links_le_12": sum(1 for value in distances if value <= low_snp_threshold),
            "nearest_neighbor_min": min(
                (
                    nearest_neighbor_snp.get(case_id)
                    for case_id in sequenced_members
                    if nearest_neighbor_snp.get(case_id) is not None
                ),
                default=None,
            ),
        }

    high_confidence_edges = [
        edge
        for edge in transmission_edges
        if float(edge.get("probability") or 0.0) >= high_posterior_threshold
    ]
    high_confidence_all_count = len(high_confidence_edges)
    high_confidence_snapshot_count = int(
        ((transmission_data or {}).get("high_confidence_edges", 0) if transmission_data else 0) or 0
    )

    qc_status_counts = {
        "pass": 0,
        "fail": 0,
        "not_reported": 0,
        "contamination": 0,
    }
    for row in case_rows:
        status = str(row.get("qc_status") or "not_reported").lower()
        contamination = bool(row.get("contamination_flag"))
        if contamination:
            qc_status_counts["contamination"] += 1
        if status in ("pass", "passed"):
            qc_status_counts["pass"] += 1
        elif status in ("", "not_reported", "na", "n/a", "unknown"):
            qc_status_counts["not_reported"] += 1
        else:
            qc_status_counts["fail"] += 1

    excluded_from_outbreaker = sum(
        1
        for row in case_rows
        if str(row.get("qc_status") or "").lower() not in ("pass", "passed")
        or bool(row.get("contamination_flag"))
    )

    cluster_action_rows = _cluster_action_rows(db)
    cluster_epi_rows = []
    for row in _cluster_epi_rows(db):
        cluster_id = str(row.get("cluster_id") or "")
        enriched_row = dict(row)
        enriched_row["lineage_distribution"] = _lineage_distribution_text(
            synthesis_cluster_by_id,
            cluster_id,
        )
        enriched_row["transmission_generations"] = _transmission_generation_text(
            synthesis_cluster_by_id,
            cluster_id,
        )
        cluster_epi_rows.append(enriched_row)

    genomic_pairs: list[RowDict] = []
    model_only_pairs: list[RowDict] = []
    qc_resolution_pairs: list[RowDict] = []
    genomically_discordant: list[RowDict] = []
    for edge in sorted(
        high_confidence_edges,
        key=lambda item: float(item.get("probability") or 0.0),
        reverse=True,
    )[:40]:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        probability = float(edge.get("probability") or 0.0)
        source_case = case_by_id.get(source) or {}
        target_case = case_by_id.get(target) or {}
        source_cluster = str(source_case.get("cluster_id") or "")
        target_cluster = str(target_case.get("cluster_id") or "")
        same_cluster = bool(source_cluster and source_cluster == target_cluster)
        pairwise_distance = pairwise_snp_matrix.get(_pair_key(source, target))
        source_qc = str(source_case.get("qc_status") or "not_reported")
        target_qc = str(target_case.get("qc_status") or "not_reported")
        qc_problem = (
            source_qc.lower() not in ("pass", "passed")
            or target_qc.lower() not in ("pass", "passed")
            or bool(source_case.get("contamination_flag"))
            or bool(target_case.get("contamination_flag"))
        )
        validation_flag = (
            "SNP-linked"
            if pairwise_distance is not None
            and pairwise_distance <= low_snp_threshold
            and same_cluster
            and not qc_problem
            else (
                "QC-unresolved"
                if qc_problem
                else (
                    f"D1: SNP>{low_snp_threshold}"
                    if pairwise_distance is not None and pairwise_distance > low_snp_threshold
                    else "Model-only"
                )
            )
        )
        confidence_tier = _confidence_tier(
            qc_status=source_qc,
            contamination_flag=bool(source_case.get("contamination_flag")),
            pairwise_distance=pairwise_distance,
            outbreaker_probability=probability,
            same_cluster=same_cluster,
        )
        synthesis_pair = (
            synthesis_pair_by_directed.get((source, target))
            or synthesis_pair_by_unordered.get(tuple(sorted([source, target])))
            or {}
        )
        synthesis_confidence = str(
            synthesis_pair.get("confidence")
            or synthesis_pair.get("confidence_code")
            or confidence_tier
        )
        synthesis_priority = synthesis_pair.get("priority_score")
        try:
            synthesis_priority_text = str(int(round(float(synthesis_priority))))
        except Exception:
            synthesis_priority_text = "n/a"

        source_lineage = str(source_case.get("lineage") or "n/a")
        target_lineage = str(target_case.get("lineage") or "n/a")
        source_major_lineage = _major_lineage(source_lineage) if source_lineage != "n/a" else ""
        target_major_lineage = _major_lineage(target_lineage) if target_lineage != "n/a" else ""
        lineage_match = (
            "yes"
            if source_major_lineage and source_major_lineage == target_major_lineage
            else ("no" if source_major_lineage and target_major_lineage else "unknown")
        )
        source_resistance = _resistance_profile_text(source_case.get("predicted_drug_resistance"))
        target_resistance = _resistance_profile_text(target_case.get("predicted_drug_resistance"))
        resistance_match = (
            "yes"
            if source_resistance != "none" and source_resistance == target_resistance
            else (
                "unknown"
                if source_resistance == "none" or target_resistance == "none"
                else "no"
            )
        )

        synthesis_flags = (
            synthesis_pair.get("flags") if isinstance(synthesis_pair.get("flags"), list) else []
        )
        concordance_bits = []
        if synthesis_pair.get("lineage_concordance"):
            concordance_bits.append(f"Lineage: {synthesis_pair.get('lineage_concordance')}")
        if synthesis_pair.get("resistance_profile_concordance"):
            concordance_bits.append(
                f"DR: {synthesis_pair.get('resistance_profile_concordance')}"
            )

        record = {
            "source": source,
            "target": target,
            "source_display": _short_case_id(source),
            "target_display": _short_case_id(target),
            "pair": f"{_short_case_id(source)}->{_short_case_id(target)}",
            "pair_html": f"{_short_case_id(source)}\u2192{_short_case_id(target)}",
            "posterior": probability,
            "pairwise": str(pairwise_distance) if pairwise_distance is not None else "n/a",
            "qc": f"{source_qc}/{target_qc}",
            "validation_flag": validation_flag,
            "confidence_tier": confidence_tier,
            "synthesis_confidence": synthesis_confidence,
            "synthesis_priority": synthesis_priority_text,
            "synthesis_concordance": "; ".join(concordance_bits) or "n/a",
            "synthesis_flags": ", ".join(
                str(flag).replace("_", " ") for flag in synthesis_flags[:3]
            )
            or "none",
            "lineage_resistance": f"{lineage_match}/{resistance_match}",
            "epi_link": "same cluster" if same_cluster else "not confirmed",
            "action_owner": "TB MDT" if not qc_problem else "Laboratory",
            "due_date": "next MDT" if not qc_problem else "48h",
        }
        if qc_problem:
            qc_resolution_pairs.append(record)
        elif pairwise_distance is not None and pairwise_distance <= low_snp_threshold and same_cluster:
            genomic_pairs.append(record)
        elif pairwise_distance is not None and pairwise_distance > low_snp_threshold:
            genomically_discordant.append(record)
        else:
            model_only_pairs.append(record)

    discordant_pairs: list[RowDict] = []
    for pair_key in sequence_pair_set.union(set(outbreaker_pair_prob.keys())):
        in_sequence = pair_key in sequence_pair_set
        in_outbreaker = pair_key in outbreaker_pair_prob
        if in_sequence == in_outbreaker:
            continue
        left, right = pair_key
        posterior = float(outbreaker_pair_prob.get(pair_key) or 0.0)
        pairwise_distance = pairwise_snp_matrix.get(pair_key)
        interpretation = (
            "Temporal support without pairwise SNP support; assess missing intermediates/importation"
            if in_outbreaker and not in_sequence
            else "Pairwise SNP support without directed outbreaker linkage; possible older/shared-source linkage"
        )
        if in_outbreaker and not in_sequence:
            disc_code = (
                "D1"
                if pairwise_distance is not None and int(pairwise_distance) > low_snp_threshold
                else "D2"
            )
        else:
            disc_code = "D3"
        discordant_pairs.append(
            {
                "pair": f"{_short_case_id(left)}-{_short_case_id(right)}",
                "snp_result": (
                    f"pairwise<={low_snp_threshold}"
                    if in_sequence
                    else f"pairwise>{low_snp_threshold} or unavailable"
                ),
                "out_result": "linked" if in_outbreaker else "not_linked",
                "pairwise": pairwise_distance,
                "posterior": posterior,
                "interpretation": interpretation,
                "disc_code": disc_code,
            }
        )

    return OutbreakReportData(
        total_cases=int(total_cases or 0),
        clustered_cases=int(clustered_cases or 0),
        open_clusters=int(open_clusters or 0),
        summary_data=summary_data if isinstance(summary_data, dict) else None,
        transmission_data=transmission_data if isinstance(transmission_data, dict) else None,
        decycled_consensus_data=decycled_consensus_data
        if isinstance(decycled_consensus_data, dict)
        else None,
        synthesis_data=synthesis_data if isinstance(synthesis_data, dict) else {},
        lineage_dr_data=lineage_dr_data if isinstance(lineage_dr_data, dict) else None,
        resistance_validation_data=resistance_validation_data
        if isinstance(resistance_validation_data, dict)
        else None,
        secondary_validation_data=secondary_validation_data
        if isinstance(secondary_validation_data, dict)
        else None,
        method_comparison_data=method_comparison_data
        if isinstance(method_comparison_data, dict)
        else None,
        sequence_summary_data=sequence_summary_data
        if isinstance(sequence_summary_data, dict)
        else None,
        fasta_analysis_data=fasta_analysis_data if isinstance(fasta_analysis_data, dict) else None,
        synthesis_pairs=synthesis_pairs,
        synthesis_parameters=synthesis_parameters,
        low_snp_threshold=low_snp_threshold,
        high_snp_contradiction_threshold=high_snp_contradiction_threshold,
        high_posterior_threshold=high_posterior_threshold,
        high_posterior_label=high_posterior_label,
        summary_provenance=summary_provenance,
        synthesis_format_version=synthesis_format_version,
        kpi_data=kpi_data if isinstance(kpi_data, dict) else None,
        qc_table_exists=qc_table_exists,
        weekly_trends=weekly_trends,
        case_rows=case_rows,
        case_by_id=case_by_id,
        sequence_by_case=sequence_by_case,
        pairwise_snp_matrix=pairwise_snp_matrix,
        transmission_edges=transmission_edges,
        enriched_network_edges=enriched_network_edges,
        enriched_high_confidence_edges=enriched_high_confidence_edges,
        best_incoming=best_incoming,
        best_outgoing=best_outgoing,
        outbreaker_pair_prob=outbreaker_pair_prob,
        high_confidence_edges=high_confidence_edges,
        high_confidence_all_count=high_confidence_all_count,
        high_confidence_snapshot_count=high_confidence_snapshot_count,
        sequence_cluster_members=sequence_cluster_members,
        sequence_pair_set=sequence_pair_set,
        nearest_neighbor_snp=nearest_neighbor_snp,
        cluster_pairwise_stats=cluster_pairwise_stats,
        qc_status_counts=qc_status_counts,
        excluded_from_outbreaker=excluded_from_outbreaker,
        cluster_action_rows=cluster_action_rows,
        cluster_epi_rows=cluster_epi_rows,
        genomic_pairs=genomic_pairs,
        model_only_pairs=model_only_pairs,
        genomically_discordant=genomically_discordant,
        qc_resolution_pairs=qc_resolution_pairs,
        discordant_pairs=discordant_pairs,
    )


def outbreak_report_snapshot(data: OutbreakReportData) -> JsonDict:
    return _jsonable(
        {
            "totals": {
                "total_cases": data.total_cases,
                "clustered_cases": data.clustered_cases,
                "open_clusters": data.open_clusters,
            },
            "thresholds": {
                "low_snp_threshold": data.low_snp_threshold,
                "high_snp_contradiction_threshold": data.high_snp_contradiction_threshold,
                "high_posterior_threshold": data.high_posterior_threshold,
                "high_posterior_label": data.high_posterior_label,
            },
            "provenance": {
                "summary_provenance": data.summary_provenance,
                "synthesis_format_version": data.synthesis_format_version,
            },
            "qc_status_counts": data.qc_status_counts,
            "excluded_from_outbreaker": data.excluded_from_outbreaker,
            "weekly_trends": data.weekly_trends,
            "cluster_action_rows": data.cluster_action_rows,
            "cluster_epi_rows": data.cluster_epi_rows,
            "genomic_pairs": data.genomic_pairs,
            "model_only_pairs": data.model_only_pairs,
            "genomically_discordant": data.genomically_discordant,
            "qc_resolution_pairs": data.qc_resolution_pairs,
            "discordant_pairs": data.discordant_pairs,
        }
    )


def _run_scalar_query(db: Session, sql: str) -> Any:
    try:
        return db.execute(text(sql)).scalar() or 0
    except Exception:
        db.rollback()
        return 0


def _run_mapping_query(db: Session, sql: str) -> list[RowDict]:
    try:
        return [dict(row) for row in db.execute(text(sql)).mappings().all()]
    except Exception:
        db.rollback()
        return []


def _weekly_trends(db: Session, qc_table_exists: bool) -> list[RowDict]:
    if qc_table_exists:
        sql = """
            WITH week_windows AS (
                SELECT (DATE_TRUNC('week', CURRENT_DATE) - (s * INTERVAL '7 days'))::date AS week_start
                FROM generate_series(11, 0, -1) s
            ),
            weekly AS (
                SELECT
                    week_windows.week_start,
                    COUNT(c.pseudonymised_case_id)::int AS eligible_cases,
                    COUNT(cs.sample_id)::int AS sequenced_cases,
                    COUNT(sqm.sample_id)::int AS qc_reported_cases,
                    COUNT(*) FILTER (
                        WHERE LOWER(COALESCE(sqm.qc_status, '')) IN ('pass', 'passed')
                    )::int AS qc_pass_cases
                FROM week_windows
                LEFT JOIN cases c
                    ON c.specimen_date >= week_windows.week_start
                    AND c.specimen_date < week_windows.week_start + INTERVAL '7 days'
                LEFT JOIN consensus_sequences cs ON cs.sample_id = c.pseudonymised_case_id
                LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id = c.pseudonymised_case_id
                GROUP BY week_windows.week_start
            )
            SELECT
                week_start,
                eligible_cases,
                sequenced_cases,
                CASE
                    WHEN eligible_cases = 0 THEN NULL
                    ELSE ROUND((sequenced_cases::numeric / eligible_cases::numeric) * 100.0, 2)
                END AS sequenced_pct,
                CASE
                    WHEN qc_reported_cases = 0 THEN NULL
                    ELSE ROUND((qc_pass_cases::numeric / qc_reported_cases::numeric) * 100.0, 2)
                END AS qc_pass_pct
            FROM weekly
            ORDER BY week_start
        """
    else:
        sql = """
            WITH week_windows AS (
                SELECT (DATE_TRUNC('week', CURRENT_DATE) - (s * INTERVAL '7 days'))::date AS week_start
                FROM generate_series(11, 0, -1) s
            ),
            weekly AS (
                SELECT
                    week_windows.week_start,
                    COUNT(c.pseudonymised_case_id)::int AS eligible_cases,
                    COUNT(cs.sample_id)::int AS sequenced_cases
                FROM week_windows
                LEFT JOIN cases c
                    ON c.specimen_date >= week_windows.week_start
                    AND c.specimen_date < week_windows.week_start + INTERVAL '7 days'
                LEFT JOIN consensus_sequences cs ON cs.sample_id = c.pseudonymised_case_id
                GROUP BY week_windows.week_start
            )
            SELECT
                week_start,
                eligible_cases,
                sequenced_cases,
                CASE
                    WHEN eligible_cases = 0 THEN NULL
                    ELSE ROUND((sequenced_cases::numeric / eligible_cases::numeric) * 100.0, 2)
                END AS sequenced_pct,
                NULL::numeric AS qc_pass_pct
            FROM weekly
            ORDER BY week_start
        """
    return _run_mapping_query(db, sql)


def _case_rows(db: Session) -> list[RowDict]:
    return _run_mapping_query(
        db,
        """
            SELECT
                c.pseudonymised_case_id::text AS case_id,
                c.specimen_date,
                c.geographic_region,
                c.case_status,
                cc.cluster_id::text AS cluster_id,
                cl.snp_distance,
                cl.investigation_status,
                ti.lineage,
                ti.predicted_drug_resistance,
                ti.resistance_mutations,
                ti.interpretation_summary,
                cs.sequence,
                sqm.qc_status,
                sqm.coverage_breadth,
                sqm.mean_depth,
                sqm.contamination_flag,
                sqm.ambiguous_base_percent
            FROM cases c
            LEFT JOIN case_clusters cc ON cc.sample_id = c.pseudonymised_case_id
            LEFT JOIN clusters cl ON cl.cluster_id = cc.cluster_id
            LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
            LEFT JOIN consensus_sequences cs ON cs.sample_id = c.pseudonymised_case_id
            LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id = c.pseudonymised_case_id
            ORDER BY c.specimen_date DESC NULLS LAST, c.pseudonymised_case_id
        """,
    )


def _cluster_action_rows(db: Session) -> list[RowDict]:
    return _run_mapping_query(
        db,
        """
            WITH cluster_stats AS (
                SELECT
                    cc.cluster_id,
                    COUNT(*)::int AS case_count,
                    MAX(c.specimen_date) AS most_recent_specimen,
                    COUNT(DISTINCT c.geographic_region)::int AS region_count,
                    COALESCE(cl.investigation_status, 'unknown') AS investigation_status,
                    EXTRACT(DAY FROM (CURRENT_DATE::timestamp - MAX(c.specimen_date)::timestamp))::int AS recency_days
                FROM case_clusters cc
                JOIN cases c ON c.pseudonymised_case_id = cc.sample_id
                LEFT JOIN clusters cl ON cl.cluster_id = cc.cluster_id
                GROUP BY cc.cluster_id, cl.investigation_status
            )
            SELECT
                cluster_id,
                case_count,
                region_count,
                most_recent_specimen,
                recency_days,
                investigation_status,
                (
                    (case_count * 2)
                    + (region_count * 3)
                    + CASE
                        WHEN recency_days <= 14 THEN 3
                        WHEN recency_days <= 30 THEN 2
                        WHEN recency_days <= 60 THEN 1
                        ELSE 0
                    END
                    + CASE WHEN investigation_status = 'open' THEN 3 ELSE 0 END
                )::int AS priority_score
            FROM cluster_stats
            ORDER BY priority_score DESC, case_count DESC, most_recent_specimen DESC
            LIMIT 10
        """,
    )


def _cluster_epi_rows(db: Session) -> list[RowDict]:
    return _run_mapping_query(
        db,
        """
            WITH base AS (
                SELECT
                    cc.cluster_id::text AS cluster_id,
                    c.pseudonymised_case_id::text AS case_id,
                    c.specimen_date,
                    c.geographic_region,
                    COALESCE(cl.investigation_status, 'unknown') AS investigation_status,
                    cl.snp_distance,
                    LOWER(CAST(ti.predicted_drug_resistance AS text)) AS dr_text
                FROM case_clusters cc
                JOIN cases c ON c.pseudonymised_case_id = cc.sample_id
                LEFT JOIN clusters cl ON cl.cluster_id = cc.cluster_id
                LEFT JOIN tb_interpretation ti ON ti.sample_id = cc.sample_id
            ),
            index_case AS (
                SELECT DISTINCT ON (cluster_id)
                    cluster_id,
                    case_id AS suspected_index_case
                FROM base
                ORDER BY cluster_id, specimen_date ASC NULLS LAST, case_id
            )
            SELECT
                base.cluster_id,
                COUNT(*)::int AS cases,
                MIN(base.specimen_date) AS first_specimen,
                MAX(base.specimen_date) AS latest_specimen,
                ROUND(AVG(COALESCE(base.snp_distance, 0))::numeric, 1) AS median_snp_proxy,
                MAX(COALESCE(base.snp_distance, 0))::int AS max_snp_proxy,
                COUNT(*) FILTER (
                    WHERE base.dr_text LIKE '%rifamp%'
                    AND (base.dr_text LIKE '%resistant%' OR base.dr_text LIKE '%"r"%')
                )::int AS rr_cases,
                COUNT(*) FILTER (
                    WHERE base.dr_text LIKE '%rifamp%'
                    AND base.dr_text LIKE '%isoniazid%'
                    AND (base.dr_text LIKE '%resistant%' OR base.dr_text LIKE '%"r"%')
                )::int AS mdr_cases,
                COUNT(*) FILTER (WHERE base.specimen_date >= CURRENT_DATE - INTERVAL '30 days')::int AS recent_30d,
                COUNT(*) FILTER (WHERE base.specimen_date >= CURRENT_DATE - INTERVAL '60 days')::int AS recent_60d,
                COUNT(*) FILTER (WHERE base.specimen_date >= CURRENT_DATE - INTERVAL '90 days')::int AS recent_90d,
                MAX(base.investigation_status) AS investigation_status,
                index_case.suspected_index_case
            FROM base
            LEFT JOIN index_case ON index_case.cluster_id = base.cluster_id
            GROUP BY base.cluster_id, index_case.suspected_index_case
            ORDER BY cases DESC, latest_specimen DESC
            LIMIT 20
        """,
    )


def _transmission_edges(transmission_data: JsonDict | None) -> list[JsonDict]:
    edges = (
        (transmission_data or {}).get("edges")
        or (transmission_data or {}).get("transmission_edges")
        or []
    )
    return [edge for edge in edges if isinstance(edge, dict)]


def _enrich_network_edges(transmission_edges: list[JsonDict]) -> list[JsonDict]:
    enriched_network_edges: list[JsonDict] = []
    for edge in transmission_edges:
        stability = edge.get("chain_stability") if isinstance(edge.get("chain_stability"), dict) else {}
        enriched = dict(edge)
        enriched["source_display"] = _short_case_id(str(edge.get("source") or ""))
        enriched["target_display"] = _short_case_id(str(edge.get("target") or ""))
        enriched["credibility_class"] = edge.get("credibility_class") or edge.get("confidence")
        enriched["posterior_entropy"] = edge.get("posterior_entropy")
        enriched["chain_agreement"] = stability.get("top_ancestor_agreement")
        enriched["probability_range_by_chain"] = (
            f"{stability.get('probability_min')} - {stability.get('probability_max')}"
            if stability.get("probability_min") is not None
            and stability.get("probability_max") is not None
            else "n/a"
        )
        enriched_network_edges.append(enriched)
    return enriched_network_edges


def _lineage_distribution_text(synthesis_cluster_by_id: dict[str, JsonDict], cluster_id: str) -> str:
    cluster = synthesis_cluster_by_id.get(str(cluster_id or ""))
    distribution = cluster.get("lineage_distribution") if isinstance(cluster, dict) else {}
    if not isinstance(distribution, dict) or not distribution:
        return "n/a"
    items = sorted(distribution.items(), key=lambda item: (-int(item[1] or 0), str(item[0])))
    return ", ".join(f"{key}: {value}" for key, value in items[:4])


def _transmission_generation_text(
    synthesis_cluster_by_id: dict[str, JsonDict],
    cluster_id: str,
) -> str:
    cluster = synthesis_cluster_by_id.get(str(cluster_id or ""))
    summary = cluster.get("summary") if isinstance(cluster, dict) else {}
    transmission_generations = summary.get("transmission_generations") if isinstance(summary, dict) else {}
    if not isinstance(transmission_generations, dict):
        return "n/a"
    max_generation = transmission_generations.get("max_generation")
    sustained = bool(transmission_generations.get("sustained_transmission_flag"))
    if max_generation is None:
        return "n/a"
    return f"{'Yes' if sustained else 'No'} (max {max_generation})"


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value
