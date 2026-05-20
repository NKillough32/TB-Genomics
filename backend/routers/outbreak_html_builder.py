import base64
import csv
import json
import logging
import os
import re
import html as html_lib
from datetime import datetime
from itertools import combinations
from pathlib import Path
from statistics import median

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.quality_gates import build_workflow_status
from backend.routers.case_overview import surveillance_kpis
from backend.routers.cases import (
    _confidence_tier,
    _drug_gene_status_label,
    _export_path,
    _get_latest_signoff,
    _image_data_uri,
    _iter_resistance_mutations,
    _lineage_analysis_summary,
    _lineage_epi_summary,
    _load_export_csv,
    _load_export_json,
    _pair_key,
    _pairwise_matrix,
    _pct,
    _pct_label,
    _resistance_profile_text,
    _resistance_report_status_label,
    _resistance_validation_key,
    _resistance_validation_lookup,
    _safe_html,
    _short_case_id,
    _write_csv_rows,
)
from backend.synthesis.transmission_synthesis import SYNTHESIS_FORMAT_VERSION

logger = logging.getLogger(__name__)


def _build_outbreak_report_html(db: Session, full: bool = False) -> str:  # noqa: C901
    """Build a rich, PDF-aligned static HTML outbreak report from database counts and export artifacts."""
    import datetime as _dt

    os.makedirs(_export_path(), exist_ok=True)

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    today = _dt.date.today()

    # -- Basic DB counts --------------------------------------------------------
    total_cases = db.execute(text("SELECT COUNT(*) FROM cases")).scalar() or 0
    clustered_cases = db.execute(text("SELECT COUNT(DISTINCT sample_id) FROM case_clusters")).scalar() or 0
    open_clusters = db.execute(
        text("SELECT COUNT(*) FROM clusters WHERE investigation_status = 'open'")
    ).scalar() or 0

    # -- Load export JSON artifacts ---------------------------------------------
    summary_data = _load_export_json("outbreaker_summary.json")
    transmission_data = _load_export_json("transmission_network.json")
    synthesis_data = _load_export_json("synthesis_output.json")
    lineage_dr_data = _load_export_json("lineage_dr_validation.json")
    resistance_validation_data = _load_export_json("resistance_validation.json")
    secondary_validation_data = _load_export_json("secondary_engine_validation.json")
    method_comparison_data = _load_export_json("cluster_method_comparison.json")
    sequence_summary_data = _load_export_json("sequence_clustering_summary.json")

    synthesis_pairs = synthesis_data.get("pairs") if isinstance(synthesis_data, dict) else []
    if not isinstance(synthesis_pairs, list):
        synthesis_pairs = []
    synthesis_parameters = synthesis_data.get("parameters") if isinstance(synthesis_data, dict) else {}
    if not isinstance(synthesis_parameters, dict):
        synthesis_parameters = {}
    low_snp_threshold = int(synthesis_parameters.get("low_snp_threshold") or 12)
    high_snp_contradiction_threshold = int(synthesis_parameters.get("high_snp_contradiction_threshold") or 20)
    high_posterior_threshold = float(synthesis_parameters.get("high_posterior_threshold") or 0.70)
    high_posterior_label = f"{high_posterior_threshold:.2f}"
    summary_provenance = str(summary_data.get("data_provenance") or "unknown") if isinstance(summary_data, dict) else "unknown"
    synthesis_format_version = synthesis_data.get("format_version") if isinstance(synthesis_data, dict) else None
    synthesis_pair_by_directed: dict[tuple[str, str], dict] = {}
    synthesis_pair_by_unordered: dict[tuple[str, str], dict] = {}
    for pair in synthesis_pairs:
        if not isinstance(pair, dict):
            continue
        src = str(pair.get("source") or "")
        tgt = str(pair.get("target") or "")
        if not src or not tgt:
            continue
        synthesis_pair_by_directed[(src, tgt)] = pair
        synthesis_pair_by_unordered[tuple(sorted([src, tgt]))] = pair

    synthesis_clusters = synthesis_data.get("clusters") if isinstance(synthesis_data, dict) else []
    if not isinstance(synthesis_clusters, list):
        synthesis_clusters = []
    synthesis_cluster_by_id = {
        str(cluster.get("cluster_id")): cluster
        for cluster in synthesis_clusters
        if isinstance(cluster, dict) and cluster.get("cluster_id")
    }

    def _synthesis_pair_for(source: str, target: str) -> dict:
        return (
            synthesis_pair_by_directed.get((source, target))
            or synthesis_pair_by_unordered.get(tuple(sorted([source, target])))
            or {}
        )

    def _lineage_distribution_text(cluster_id: str) -> str:
        cluster = synthesis_cluster_by_id.get(str(cluster_id or ""))
        distribution = cluster.get("lineage_distribution") if isinstance(cluster, dict) else {}
        if not isinstance(distribution, dict) or not distribution:
            return "n/a"
        items = sorted(distribution.items(), key=lambda item: (-int(item[1] or 0), str(item[0])))
        return ", ".join(f"{key}: {value}" for key, value in items[:4])

    def _transmission_generation_text(cluster_id: str) -> str:
        cluster = synthesis_cluster_by_id.get(str(cluster_id or ""))
        summary = cluster.get("summary") if isinstance(cluster, dict) else {}
        tx = summary.get("transmission_generations") if isinstance(summary, dict) else {}
        if not isinstance(tx, dict):
            return "n/a"
        max_generation = tx.get("max_generation")
        sustained = bool(tx.get("sustained_transmission_flag"))
        if max_generation is None:
            return "n/a"
        return f"{'Yes' if sustained else 'No'} (max {max_generation})"

    mock_outbreaker_html = (
        '<div class="callout callout-alert" style="margin-top:.6rem"><strong>Mock outbreaker2 fallback in use.</strong> '
        'This report was generated from demonstration outbreaker output rather than a real R-based outbreaker2 run. '
        'Transmission probabilities, network directionality, and generation-depth signals must not be treated as operational evidence.</div>'
        if summary_provenance == "mock"
        else ""
    )
    synthesis_version_warning_html = ""
    if synthesis_data and synthesis_format_version is None:
        synthesis_version_warning_html = (
            '<div class="callout callout-warn" style="margin-top:.6rem"><strong>Synthesis output format review required.</strong> '
            'The loaded synthesis_output.json is missing a format_version field. Report builders are using compatibility fallbacks; regenerate synthesis output before external circulation.</div>'
        )
    elif synthesis_data and synthesis_format_version != SYNTHESIS_FORMAT_VERSION:
        synthesis_version_warning_html = (
            '<div class="callout callout-warn" style="margin-top:.6rem"><strong>Synthesis output version mismatch.</strong> '
            f'This report expects format_version {SYNTHESIS_FORMAT_VERSION} but loaded {_safe_html(str(synthesis_format_version))}. '
            'Review synthesis_output.json and regenerate exports before relying on derived cluster metrics.</div>'
        )

    def _build_analysis_quality_warnings() -> str:
        warnings: list[str] = []

        if isinstance(summary_data, dict):
            diagnostic_status = str(summary_data.get("mcmc_diagnostic_status") or "").strip()
            if diagnostic_status and "drifting" in diagnostic_status.lower():
                drift_fraction = summary_data.get("mcmc_late_drift_fraction")
                drift_text = ""
                try:
                    drift_text = f" Late-chain drift estimate: {float(drift_fraction) * 100:.1f}%."
                except Exception:
                    drift_text = ""
                warnings.append(
                    "MCMC trace is still drifting. Treat transmission probabilities as exploratory "
                    f"and repeat with longer chains before circulation.{drift_text}"
                )
            if summary_provenance == "mock":
                warnings.append(
                    "Outbreaker output provenance is mock/demo fallback. Directionality, posteriors, and chain-depth summaries are illustrative only until a real outbreaker2 run completes."
                )

        if isinstance(lineage_dr_data, dict):
            engines = lineage_dr_data.get("engines") if isinstance(lineage_dr_data.get("engines"), dict) else {}
            tb_profiler = engines.get("tb_profiler") if isinstance(engines.get("tb_profiler"), dict) else {}
            mykrobe = engines.get("mykrobe") if isinstance(engines.get("mykrobe"), dict) else {}

            tb_status = str(tb_profiler.get("status") or "").strip()
            tb_status_ok = tb_status.lower() in {"ok", "available", "completed", "installed", "success"}
            if tb_status and not tb_status_ok:
                detail = str(tb_profiler.get("error") or tb_profiler.get("message") or "").strip()
                warnings.append(
                    "TBProfiler did not complete usable validation"
                    + (f" ({tb_status}: {detail})." if detail else f" ({tb_status}).")
                )

            mykrobe_status = str(mykrobe.get("status") or "").strip()
            mykrobe_ok = mykrobe_status.lower() in {"ok", "available", "completed", "installed", "success"}
            if mykrobe_status and not mykrobe_ok:
                warnings.append(f"Mykrobe validation is unavailable ({mykrobe_status}).")

            dr_concordance = lineage_dr_data.get("dr_concordance")
            if isinstance(dr_concordance, dict):
                compared = dr_concordance.get("samples_compared")
                try:
                    compared_count = int(compared or 0)
                except Exception:
                    compared_count = 0
                if compared_count == 0:
                    warnings.append(
                        "No samples were compared for drug-resistance concordance. "
                        "Resistance heatmaps remain useful for review, but are not externally validated."
                    )

        if not synthesis_pairs:
            warnings.append(
                "Transmission synthesis output is missing. Printed confidence categories fall back to "
                "legacy report heuristics until exports/synthesis_output.json is generated."
            )
        elif synthesis_format_version is None:
            warnings.append(
                "synthesis_output.json has no format_version field. Compatibility fallbacks are active; regenerate synthesis output before external circulation."
            )
        elif synthesis_format_version != SYNTHESIS_FORMAT_VERSION:
            warnings.append(
                f"synthesis_output.json format_version {synthesis_format_version} does not match expected version {SYNTHESIS_FORMAT_VERSION}. Regenerate synthesis output before relying on cluster summaries."
            )

        if isinstance(method_comparison_data, dict):
            try:
                precision = float(method_comparison_data.get("precision_sequence_given_outbreaker"))
            except Exception:
                precision = None
            if precision is not None and precision < 0.5:
                warnings.append(
                    "Sequence cluster and outbreaker assignments have low overlap "
                    f"(precision {precision:.2f}). Review model-only links before operational escalation."
                )

        if not warnings:
            return ""

        items = "".join(f"<li>{_safe_html(item)}</li>" for item in warnings)
        return (
            '<div class="callout callout-warn" style="margin-top:.7rem">'
            '<strong>Analysis quality checks need review.</strong>'
            f'<ul class="compact-list">{items}</ul>'
            '</div>'
        )

    analysis_quality_warnings_html = _build_analysis_quality_warnings()
    workflow_quality_status = build_workflow_status(db)

    def _status_badge(status: str) -> str:
        status_text = str(status or "unknown")
        cls = {
            "pass": "badge-green",
            "ready": "badge-green",
            "available": "badge-green",
            "complete": "badge-green",
            "warn": "badge-amber",
            "warning": "badge-amber",
            "review": "badge-orange",
            "usable_with_warnings": "badge-amber",
            "incomplete": "badge-grey",
            "pending": "badge-grey",
            "missing": "badge-grey",
            "fail": "badge-red",
        }.get(status_text, "badge-grey")
        return f'<span class="badge {cls}">{_safe_html(status_text.replace("_", " ").title())}</span>'

    def _build_confidence_gate_table() -> str:
        gates = workflow_quality_status.get("gates") or []
        if not gates:
            return ""
        rows = []
        for gate in gates:
            gate_status = str(gate.get("status", "unknown"))
            gate_message = gate.get("message", "")
            gate_key = str(gate.get("key", "") or "").lower()
            gate_label = str(gate.get("label", gate.get("key", "Gate")) or "")
            if (
                ("mcmc" in gate_key or "mcmc" in gate_label.lower())
                and isinstance(summary_data, dict)
                and int(summary_data.get("n_samples") or 0) < 100
            ):
                gate_status = "review"
                gate_message = (
                    "Only "
                    f"{int(summary_data.get('n_samples') or 0)} posterior sample(s) available after burn-in. "
                    "Treat model directionality and edge probabilities as exploratory."
                )
            flags = []
            if gate.get("process_blocking"):
                flags.append("blocks process")
            if gate.get("interpretation_blocking"):
                flags.append("limits interpretation")
            flag_text = ", ".join(flags) if flags else "non-blocking"
            rows.append(
                "<tr>"
                f"<td>{_safe_html(gate.get('label', gate.get('key', 'Gate')))}</td>"
                f"<td>{_status_badge(gate_status)}</td>"
                f"<td>{_safe_html(gate_message)}</td>"
                f"<td>{_safe_html(flag_text)}</td>"
                "</tr>"
            )
        return (
            '<details open class="quality-gates"><summary>Analysis confidence gates</summary><div>'
            '<div class="section-note">Dependency gaps are warnings and do not stop the workflow. '
            'Data-quality gates show whether results should be treated as operational, limited, or incomplete.</div>'
            '<div class="tbl-wrap"><table><thead><tr><th>Gate</th><th>Status</th><th>Finding</th><th>Impact</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table></div>"
            "</div></details>"
        )

    confidence_gate_table_html = _build_confidence_gate_table()

    try:
        kpi_data = surveillance_kpis(weeks=12, db=db)
    except Exception as exc:
        kpi_data = {"warning": str(exc)}

    # -- Weekly trends ----------------------------------------------------------
    qc_table_exists = db.execute(
        text("SELECT to_regclass('public.sample_qc_metrics') IS NOT NULL")
    ).scalar()
    weekly_trends = []
    try:
        if qc_table_exists:
            weekly_trends = db.execute(text("""
                WITH ww AS (SELECT (DATE_TRUNC('week',CURRENT_DATE)-(s*INTERVAL '7 days'))::date AS ws
                            FROM generate_series(11,0,-1) s),
                wdata AS (SELECT ww.ws,COUNT(c.pseudonymised_case_id)::int AS eligible,
                    COUNT(cs.sample_id)::int AS sequenced,
                    COUNT(sqm.sample_id)::int AS qc_rep,
                    COUNT(*) FILTER(WHERE LOWER(COALESCE(sqm.qc_status,'')) IN ('pass','passed'))::int AS qc_pass
                    FROM ww LEFT JOIN cases c ON c.specimen_date>=ww.ws AND c.specimen_date<ww.ws+INTERVAL '7 days'
                    LEFT JOIN consensus_sequences cs ON cs.sample_id=c.pseudonymised_case_id
                    LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id=c.pseudonymised_case_id
                    GROUP BY ww.ws)
                SELECT ws AS week_start,eligible,sequenced,
                    CASE WHEN eligible=0 THEN NULL ELSE ROUND((sequenced::numeric/eligible::numeric)*100,1) END AS seq_pct,
                    CASE WHEN qc_rep=0 THEN NULL ELSE ROUND((qc_pass::numeric/qc_rep::numeric)*100,1) END AS qc_pct
                FROM wdata ORDER BY ws
            """)).mappings().all()
        else:
            weekly_trends = db.execute(text("""
                WITH ww AS (SELECT (DATE_TRUNC('week',CURRENT_DATE)-(s*INTERVAL '7 days'))::date AS ws
                            FROM generate_series(11,0,-1) s)
                SELECT ww.ws AS week_start,COUNT(c.pseudonymised_case_id)::int AS eligible,
                    COUNT(cs.sample_id)::int AS sequenced,
                    CASE WHEN COUNT(c.pseudonymised_case_id)=0 THEN NULL
                         ELSE ROUND((COUNT(cs.sample_id)::numeric/COUNT(c.pseudonymised_case_id)::numeric)*100,1)
                    END AS seq_pct, NULL::numeric AS qc_pct
                FROM ww LEFT JOIN cases c ON c.specimen_date>=ww.ws AND c.specimen_date<ww.ws+INTERVAL '7 days'
                LEFT JOIN consensus_sequences cs ON cs.sample_id=c.pseudonymised_case_id
                GROUP BY ww.ws ORDER BY ww.ws
            """)).mappings().all()
    except Exception:
        weekly_trends = []

    # -- Case-level detail query ------------------------------------------------
    case_rows = []
    try:
        case_rows = db.execute(text("""
            SELECT c.pseudonymised_case_id::text AS case_id, c.specimen_date, c.geographic_region,
                c.case_status, cc.cluster_id::text AS cluster_id, cl.snp_distance, cl.investigation_status,
                ti.lineage, ti.predicted_drug_resistance, ti.resistance_mutations, ti.interpretation_summary,
                cs.sequence, sqm.qc_status, sqm.coverage_breadth, sqm.mean_depth,
                sqm.contamination_flag, sqm.ambiguous_base_percent
            FROM cases c
            LEFT JOIN case_clusters cc ON cc.sample_id=c.pseudonymised_case_id
            LEFT JOIN clusters cl ON cl.cluster_id=cc.cluster_id
            LEFT JOIN tb_interpretation ti ON ti.sample_id=c.pseudonymised_case_id
            LEFT JOIN consensus_sequences cs ON cs.sample_id=c.pseudonymised_case_id
            LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id=c.pseudonymised_case_id
            ORDER BY c.specimen_date DESC NULLS LAST, c.pseudonymised_case_id
        """)).mappings().all()
    except Exception:
        case_rows = []

    case_by_id = {str(r.get("case_id")): r for r in case_rows if r.get("case_id")}
    sequence_by_case = {
        str(r.get("case_id")): str(r.get("sequence") or "").strip().upper()
        for r in case_rows if r.get("case_id") and r.get("sequence")
    }
    pairwise_snp_matrix = _pairwise_matrix(sequence_by_case)

    # -- QC status counts -------------------------------------------------------
    qc_status_counts = {"pass": 0, "fail": 0, "not_reported": 0, "contamination": 0}
    for r in case_rows:
        st = str(r.get("qc_status") or "not_reported").lower()
        if bool(r.get("contamination_flag")):
            qc_status_counts["contamination"] += 1
        if st in ("pass", "passed"):
            qc_status_counts["pass"] += 1
        elif st in ("", "not_reported", "na", "n/a", "unknown"):
            qc_status_counts["not_reported"] += 1
        else:
            qc_status_counts["fail"] += 1
    excluded_from_outbreaker = sum(
        1 for r in case_rows
        if str(r.get("qc_status") or "").lower() not in ("pass", "passed") or bool(r.get("contamination_flag"))
    )

    # -- Transmission edge data -------------------------------------------------
    transmission_edges = (transmission_data or {}).get("edges") or (transmission_data or {}).get("transmission_edges") or []
    best_incoming: dict = {}
    best_outgoing: dict = {}
    outbreaker_pair_prob: dict = {}
    for edge in transmission_edges:
        src = str(edge.get("source") or "")
        tgt = str(edge.get("target") or "")
        if not src or not tgt:
            continue
        prob = float(edge.get("probability") or 0.0)
        if tgt not in best_incoming or prob > best_incoming[tgt]["probability"]:
            best_incoming[tgt] = {"source": src, "probability": prob}
        if src not in best_outgoing or prob > best_outgoing[src]["probability"]:
            best_outgoing[src] = {"target": tgt, "probability": prob}
        pk = tuple(sorted([src, tgt]))
        if pk not in outbreaker_pair_prob or prob > outbreaker_pair_prob[pk]:
            outbreaker_pair_prob[pk] = prob

    high_confidence_edges = [e for e in transmission_edges if float(e.get("probability") or 0.0) >= high_posterior_threshold]
    high_confidence_all_count = len(high_confidence_edges)
    high_confidence_snapshot_count = int((transmission_data or {}).get("high_confidence_edges", 0) or 0)

    sequence_cluster_members: dict = {}
    for r in case_rows:
        cid = str(r.get("cluster_id") or "")
        cid_case = str(r.get("case_id") or "")
        if cid and cid_case:
            sequence_cluster_members.setdefault(cid, []).append(cid_case)

    nearest_neighbor_snp: dict = {}
    nearest_neighbor_partner: dict = {}
    for case_id, seq in sequence_by_case.items():
        relevant = []
        for other_id in sequence_by_case:
            if other_id == case_id:
                continue
            dist = pairwise_snp_matrix.get(_pair_key(case_id, other_id))
            if dist is not None:
                relevant.append((dist, other_id))
        if relevant:
            bd, bp = sorted(relevant)[0]
            nearest_neighbor_snp[case_id] = int(bd)
            nearest_neighbor_partner[case_id] = bp

    pairwise_links_le_12 = sum(1 for d in pairwise_snp_matrix.values() if d <= 12)
    sequence_pair_set = {pair for pair, d in pairwise_snp_matrix.items() if d <= 12}

    # -- Cluster action priority (from DB) -------------------------------------
    cluster_action_rows = []
    try:
        cluster_action_rows = db.execute(text("""
            WITH cs AS (SELECT cc.cluster_id,COUNT(*)::int AS case_count,
                MAX(c.specimen_date) AS most_recent_specimen,
                COUNT(DISTINCT c.geographic_region)::int AS region_count,
                COALESCE(cl.investigation_status,'unknown') AS investigation_status,
                EXTRACT(DAY FROM (CURRENT_DATE::timestamp-MAX(c.specimen_date)::timestamp))::int AS recency_days
                FROM case_clusters cc JOIN cases c ON c.pseudonymised_case_id=cc.sample_id
                LEFT JOIN clusters cl ON cl.cluster_id=cc.cluster_id
                GROUP BY cc.cluster_id,cl.investigation_status)
            SELECT cluster_id,case_count,region_count,most_recent_specimen,recency_days,investigation_status,
                ((case_count*2)+(region_count*3)+CASE WHEN recency_days<=14 THEN 3 WHEN recency_days<=30 THEN 2
                 WHEN recency_days<=60 THEN 1 ELSE 0 END+CASE WHEN investigation_status='open' THEN 3 ELSE 0 END)::int AS priority_score
            FROM cs ORDER BY priority_score DESC,case_count DESC,most_recent_specimen DESC LIMIT 10
        """)).mappings().all()
    except Exception:
        cluster_action_rows = []

    # -- Cluster epidemiology ---------------------------------------------------
    cluster_epi_rows = []
    try:
        cluster_epi_rows = db.execute(text("""
            WITH base AS (SELECT cc.cluster_id::text AS cluster_id,c.pseudonymised_case_id::text AS case_id,
                c.specimen_date,c.geographic_region,COALESCE(cl.investigation_status,'unknown') AS investigation_status,
                cl.snp_distance,LOWER(CAST(ti.predicted_drug_resistance AS text)) AS dr_text
                FROM case_clusters cc JOIN cases c ON c.pseudonymised_case_id=cc.sample_id
                LEFT JOIN clusters cl ON cl.cluster_id=cc.cluster_id
                LEFT JOIN tb_interpretation ti ON ti.sample_id=cc.sample_id),
            idx AS (SELECT DISTINCT ON(cluster_id) cluster_id,case_id AS suspected_index_case
                    FROM base ORDER BY cluster_id,specimen_date ASC NULLS LAST,case_id)
            SELECT b.cluster_id,COUNT(*)::int AS cases,
                MIN(b.specimen_date) AS first_specimen,MAX(b.specimen_date) AS latest_specimen,
                ROUND(AVG(COALESCE(b.snp_distance,0))::numeric,1) AS median_snp_proxy,
                MAX(COALESCE(b.snp_distance,0))::int AS max_snp_proxy,
                COUNT(*) FILTER(WHERE b.dr_text LIKE '%rifamp%' AND (b.dr_text LIKE '%resistant%' OR b.dr_text LIKE '%"r"%'))::int AS rr_cases,
                COUNT(*) FILTER(WHERE b.dr_text LIKE '%rifamp%' AND b.dr_text LIKE '%isoniazid%' AND (b.dr_text LIKE '%resistant%' OR b.dr_text LIKE '%"r"%'))::int AS mdr_cases,
                COUNT(*) FILTER(WHERE b.specimen_date>=CURRENT_DATE-INTERVAL '30 days')::int AS recent_30d,
                COUNT(*) FILTER(WHERE b.specimen_date>=CURRENT_DATE-INTERVAL '60 days')::int AS recent_60d,
                COUNT(*) FILTER(WHERE b.specimen_date>=CURRENT_DATE-INTERVAL '90 days')::int AS recent_90d,
                MAX(b.investigation_status) AS investigation_status, i.suspected_index_case
            FROM base b LEFT JOIN idx i ON i.cluster_id=b.cluster_id
            GROUP BY b.cluster_id,i.suspected_index_case ORDER BY cases DESC,latest_specimen DESC LIMIT 20
        """)).mappings().all()
    except Exception:
        cluster_epi_rows = []

    # -- Mutation validation rows -----------------------------------------------
    mutation_rows_raw = []
    try:
        mutation_rows_raw = db.execute(text("""
            SELECT sample_id::text AS case_id,resistance_mutations,predicted_drug_resistance
            FROM tb_interpretation WHERE resistance_mutations IS NOT NULL
            AND resistance_mutations::text NOT IN ('null','{}','[]')
            ORDER BY sample_id LIMIT 80
        """)).mappings().all()
    except Exception:
        mutation_rows_raw = []

    # -- Run-level QC -----------------------------------------------------------
    run_qc_rows_db = []
    if qc_table_exists:
        try:
            run_qc_rows_db = [dict(r._mapping) for r in db.execute(text("""
                SELECT CAST(reported_at AS DATE) AS run_date,COUNT(*) AS total_samples,
                    SUM(CASE WHEN LOWER(qc_status) IN ('pass','passed') THEN 1 ELSE 0 END) AS pass_count,
                    SUM(CASE WHEN LOWER(qc_status) NOT IN ('pass','passed') THEN 1 ELSE 0 END) AS fail_count,
                    ROUND(CAST(AVG(mean_depth) AS NUMERIC),1) AS mean_depth_avg,
                    ROUND(CAST(AVG(coverage_breadth) AS NUMERIC),1) AS mean_coverage_avg
                FROM sample_qc_metrics WHERE reported_at IS NOT NULL
                GROUP BY CAST(reported_at AS DATE) ORDER BY CAST(reported_at AS DATE) DESC LIMIT 20
            """)).fetchall()]
        except Exception:
            run_qc_rows_db = []

    # -- Reproducibility / pipeline metadata variables -------------------------
    # Pull from DB tables first, fall back to JSON artifact values.
    _seq_run_row: dict = {}
    try:
        _seq_run_row = dict(db.execute(text(
            "SELECT platform, instrument_name, pipeline_version, reference_genome, completed_at "
            "FROM sequencing_runs ORDER BY created_at DESC NULLS LAST LIMIT 1"
        )).mappings().first() or {})
    except Exception:
        db.rollback()

    _prov_row_db: dict = {}
    try:
        _prov_row_db = dict(db.execute(text(
            "SELECT pipeline_name, pipeline_version, reference_genome, software_versions, parameters, generated_at "
            "FROM analysis_provenance ORDER BY generated_at DESC NULLS LAST LIMIT 1"
        )).mappings().first() or {})
    except Exception:
        db.rollback()

    def _parse_jsonb(val: object) -> dict:
        """JSONB columns come back from SQLAlchemy as dicts already; strings need loads()."""
        if isinstance(val, dict):
            return val
        if val is None:
            return {}
        try:
            import json as _j
            result = _j.loads(val)
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}

    _sw: dict = _parse_jsonb(_prov_row_db.get("software_versions"))
    _params: dict = _parse_jsonb(_prov_row_db.get("parameters"))

    ref_genome     = (_prov_row_db.get("reference_genome") or _seq_run_row.get("reference_genome")
                      or (summary_data or {}).get("reference_genome"))
    seq_platform   = _seq_run_row.get("platform") or _params.get("platform")
    instrument     = _seq_run_row.get("instrument_name") or _params.get("instrument")
    library_prep   = _params.get("library_prep") or _params.get("library_preparation")
    snp_pipeline   = (_prov_row_db.get("pipeline_name") or _prov_row_db.get("pipeline_version")
                      or (summary_data or {}).get("snp_pipeline_version"))
    mapping_tool   = _sw.get("mapper") or _sw.get("bwa") or _params.get("mapper")
    variant_caller = _sw.get("variant_caller") or _params.get("variant_caller")
    snp_threshold  = str(_params.get("snp_threshold") or 12)
    resist_cat     = (_sw.get("resistance_catalogue") or _params.get("resistance_catalogue")
                      or (lineage_dr_data or {}).get("resistance_catalogue_version"))
    lineage_tool   = (_sw.get("lineage_tool") or _params.get("lineage_tool")
                      or (lineage_dr_data or {}).get("lineage_tool_version"))
    outbreaker_ver = (_sw.get("outbreaker2") or _params.get("outbreaker_version")
                      or (summary_data or {}).get("analysis_engine_version"))
    random_seed    = (str(_params.get("random_seed") or "")
                      or str((summary_data or {}).get("random_seed") or "") or None)
    gen_time_mean  = str(_params.get("gen_time_mean") or _params.get("generation_time_mean") or "")
    gen_time_sd    = str(_params.get("gen_time_sd") or _params.get("generation_time_sd") or "")
    sampling_prob  = str(_params.get("sampling_prob") or _params.get("pi") or "")
    pipeline_run_date = str(_seq_run_row.get("completed_at") or "").strip() or None
    analysis_provenance_date = str(_prov_row_db.get("generated_at") or "").strip() or None

    # -- Pipeline validation sign-off -------------------------------------------
    _signoff = _get_latest_signoff(db)
    if _signoff and _signoff.get("decision") == "approved":
        _pipeline_valid_badge = (
            f'<span class="badge badge-green">Approved &#8212; '
            f'{_safe_html(str(_signoff["reviewer"]))} &middot; '
            f'{_safe_html(str(_signoff["signed_off_at"])[:10])}</span>'
        )
    elif _signoff and _signoff.get("decision") == "rejected":
        _pipeline_valid_badge = (
            f'<span class="badge badge-red">Sign-off rejected &#8212; '
            f'{_safe_html(str(_signoff["reviewer"]))} &middot; '
            f'{_safe_html(str(_signoff["signed_off_at"])[:10])}</span>'
        )
    else:
        _pipeline_valid_badge = '<span class="badge badge-red">Not validated &#8212; under review</span>'

    _resist_cat_html = _safe_html(resist_cat or 'Not recorded &#8212; required')

    # -- Resistance blocking card (dynamic based on sign-off) -------------------
    # Shared sign-off form - appended to the card in both states.
    _signoff_form_html = (
        '<div id="signoff-panel" style="margin-top:.9rem;padding:.8rem 1rem;'
        'background:rgba(0,0,0,.03);border-radius:6px;border:1px solid rgba(0,0,0,.1)">'
        '<strong style="font-size:.9rem">Record pipeline validation sign-off</strong>'
        '<form id="signoff-form" style="margin-top:.6rem;display:grid;gap:.45rem">'
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:.45rem">'
        '<input id="sf-reviewer" type="text" placeholder="Reviewer name *" required '
        'style="padding:.35rem .6rem;border:1px solid #ced4da;border-radius:4px;font-size:.87rem">'
        '<select id="sf-decision" '
        'style="padding:.35rem .6rem;border:1px solid #ced4da;border-radius:4px;font-size:.87rem">'
        '<option value="approved">Approved</option>'
        '<option value="rejected">Rejected</option>'
        '<option value="under_review">Under review</option>'
        '</select>'
        '</div>'
        '<input id="sf-cat" type="text" placeholder="Catalogue / version (e.g. WHO 2022)" '
        'style="padding:.35rem .6rem;border:1px solid #ced4da;border-radius:4px;font-size:.87rem">'
        '<textarea id="sf-notes" placeholder="Notes" rows="2" '
        'style="padding:.35rem .6rem;border:1px solid #ced4da;border-radius:4px;'
        'font-size:.87rem;resize:vertical"></textarea>'
        '<div style="display:flex;align-items:center;gap:.7rem">'
        '<button type="submit" '
        'style="background:#1d4ed8;color:#fff;border:none;padding:.38rem 1rem;'
        'border-radius:4px;font-size:.87rem;cursor:pointer">Submit sign-off</button>'
        '<span id="sf-msg" style="font-size:.84rem;color:#374151"></span>'
        '</div>'
        '</form>'
        '</div>'
        '<script>'
        'document.getElementById("signoff-form").addEventListener("submit",async function(e){'
        'e.preventDefault();'
        'var msg=document.getElementById("sf-msg");'
        'msg.textContent="Submitting\u2026";'
        'try{'
        'var r=await fetch("/cases/resistance-validation/approve",{'
        'method:"POST",'
        'headers:{"Content-Type":"application/json"},'
        'body:JSON.stringify({'
        'decision:document.getElementById("sf-decision").value,'
        'reviewer:document.getElementById("sf-reviewer").value,'
        'notes:document.getElementById("sf-notes").value,'
        'catalogue_version:document.getElementById("sf-cat").value'
        '})'
        '});'
        'if(r.ok){'
        'msg.textContent="\u2713 Sign-off recorded \u2014 reloading\u2026";'
        'msg.style.color="#16a34a";'
        'setTimeout(()=>location.reload(),1400);'
        '}else{'
        'var err=await r.json().catch(()=>({}));'
        'msg.textContent="Error: "+(err.detail||r.statusText);'
        'msg.style.color="#dc2626";'
        '}'
        '}catch(ex){'
        'msg.textContent="Network error: "+ex.message;'
        'msg.style.color="#dc2626";'
        '}'
        '});'
        '</script>'
    )

    if _signoff and _signoff.get("decision") == "approved":
        _signoff_note_parts = [
            f'Pipeline validation approved by <strong>{_safe_html(str(_signoff["reviewer"]))}</strong>'
            f' on {_safe_html(str(_signoff["signed_off_at"])[:10])}.'
        ]
        if _signoff.get("notes"):
            _signoff_note_parts.append(f' Reviewer notes: {_safe_html(str(_signoff["notes"]))}')
        _signoff_note = "".join(_signoff_note_parts)
        _blocking_card_html = (
            f'<div class="card card-warn" style="border-width:2px;padding:1rem;margin-bottom:.9rem">'
            f'<strong>&#9888; Pipeline validation sign-off recorded.</strong><br>'
            f'Unusual gene-drug mappings were detected and reviewed. {_signoff_note}'
            f'<ul style="margin:.5rem 0 .4rem 1.2rem;font-size:.87rem">'
            f'<li>Resistance calls with <span class="badge badge-red">Unusual gene-drug mapping</span>'
            f' remain <strong><span class="badge badge-red">Suppressed</span></strong> &#8212; '
            f'a full bioinformatics audit is required before any clinical use.</li>'
            f'<li>Resistance calls with <span class="badge badge-green">Valid expected gene</span>'
            f' are classified as <strong><span class="badge badge-amber">Not validated</span></strong>'
            f' &#8212; require phenotypic DST confirmation before clinical use.</li>'
            f'</ul>'
            f'<span style="font-size:.82rem;color:var(--muted)">Resistance catalogue/version: '
            f'<strong>{_resist_cat_html}</strong>'
            f' &nbsp;&middot;&nbsp; Local pipeline validation status: {_pipeline_valid_badge}</span>'
            f'<details style="margin-top:.7rem"><summary style="cursor:pointer;font-size:.87rem;color:#1d4ed8">Revise sign-off</summary>'
            f'{_signoff_form_html}'
            f'</details>'
            f'</div>'
        )
    else:
        _blocking_card_html = (
            f'<div class="card card-alert" style="border-width:2px;padding:1rem;margin-bottom:.9rem">'
            f'<strong>&#128683; BLOCKING SAFETY ISSUE &#8212; Local drug-resistance pipeline '
            f'validation required before any clinical or operational use.</strong><br>'
            f'This is an internal report safety screen, not wording or case-level interpretation '
            f'supplied directly by WHO. Unusual gene-drug mappings have been detected '
            f'(e.g. <em>gyrA</em> linked to pyrazinamide, <em>embB</em> linked to fluoroquinolones, '
            f'<em>pncA</em> linked to rifampicin). These indicate a likely local bioinformatics '
            f'pipeline data-mapping error.'
            f'<ul style="margin:.5rem 0 .4rem 1.2rem;font-size:.87rem">'
            f'<li>All resistance calls with <span class="badge badge-red">Unusual gene-drug mapping</span>'
            f' are classified by this local report as <strong><span class="badge badge-red">Suppressed</span></strong>'
            f' &#8212; must not be reported, acted on, or shared until the pipeline is validated.</li>'
            f'<li>Resistance calls with <span class="badge badge-green">Valid expected gene</span>'
            f' are classified by this local report as <strong><span class="badge badge-amber">Not validated</span></strong>'
            f' &#8212; require phenotypic DST confirmation before clinical use.</li>'
            f'<li>No resistance call in this report should be treated as <strong>Validated</strong>'
            f' until a formal pipeline audit is complete and phenotypic DST results are available.</li>'
            f'</ul>'
            f'Quarantine all affected samples. Notify the bioinformatics lead immediately. '
            f'Do not include resistance findings from this report in patient records or MDT summaries until resolved.<br>'
            f'<span style="font-size:.82rem;color:var(--muted)">Configured resistance catalogue/version metadata: '
            f'<strong>{_resist_cat_html}</strong>'
            f' &nbsp;&middot;&nbsp; Local pipeline validation status: {_pipeline_valid_badge}</span>'
            f'{_signoff_form_html}'
            f'</div>'
        )

    # -- Reproduce metadata gate ------------------------------------------------
    # Uses the already-resolved DB variables so the DB is the source of truth.
    required_repro_metadata = [
        ("Reference genome", ref_genome),
        ("SNP-calling pipeline/version", snp_pipeline),
        ("Resistance catalogue/version", resist_cat),
        ("Lineage-calling tool/version", lineage_tool),
        ("outbreaker2 version", outbreaker_ver),
        ("Random seed", random_seed),
        ("Pipeline run date", pipeline_run_date),
        ("Analysis provenance date", analysis_provenance_date),
    ]
    missing_repro = [label for label, v in required_repro_metadata if v is None or (isinstance(v, str) and not v.strip())]
    circulation_ok = not missing_repro

    # -- Lineage epi summary ----------------------------------------------------
    analysis_summary = _lineage_analysis_summary(db)
    analysis_epi_summary = _lineage_epi_summary(db)

    # -- Graphics ---------------------------------------------------------------
    # Metadata for each known figure: stem -> (title, interpretive caption)
    _FIGURE_META: dict[str, tuple[str, str]] = {
        "outbreaker_trace": (
            "MCMC Log-Likelihood Trace",
            "Inspect for convergence: a stable horizontal band indicates good chain mixing. "
            "Visible drift, cycles, or sudden jumps suggest poor convergence - "
            "treat all model output as exploratory until convergence is confirmed.",
        ),
        "outbreaker_hist": (
            "MCMC Log-Likelihood Distribution",
            "A near-normal, unimodal histogram indicates the sampler explored the posterior well. "
            "Multi-modal or heavily skewed distributions suggest the chain has not converged - "
            "model-prioritised transmission links should be interpreted with caution.",
        ),
        "outbreaker_tree": (
            "Posterior Transmission Tree",
            "Arrows show the most probable who-infected-whom direction from outbreaker2 posterior "
            "marginal modes. Edge colour and width show posterior support; the embedded legend maps "
            "support bands and cluster colours. These are probabilistic hypotheses, not confirmed routes. "
            "Validate each link with pairwise SNP distance <=12 and epidemiological corroboration "
            "before operational action.",
        ),
        "outbreaker_phylo": (
            "Hierarchical Clustering Dendrogram (SNP Distance)",
            "Cases joined at a low branch height share recent common ancestry. "
            "Visible compact sub-trees correspond to transmission clusters. "
            "Branch heights are proportional to pairwise SNP distance - "
            "cases below the 12-SNP threshold are likely directly linked.",
        ),
        "outbreaker_resistance": (
            "Drug Resistance Profile Heatmap",
            "Rows = case isolates, columns = drug classes. "
            "Rows are sorted by resistance burden. Grey = no call, green = susceptible, "
            "yellow = intermediate, red = resistant. "
            "All genomic resistance predictions must be confirmed by phenotypic DST before clinical use.",
        ),
    }

    # Load all PNGs into a dict keyed by stem for contextual placement
    figures_by_stem: dict[str, str] = {}  # stem -> base64 data URI
    if full:
        for image_path in sorted(Path(_export_path()).glob("outbreaker_*.png")):
            uri = _image_data_uri(str(image_path))
            if uri:
                figures_by_stem[image_path.stem] = uri

    def _figure_card(stem: str, show_in_gallery: bool = False) -> str:
        """Render a single figure with interpretive caption, zoom button, and download link."""
        if not full:
            return '<p class="muted">Figure omitted from the short report. Open the full HTML report for embedded figures and downloads.</p>'
        uri = figures_by_stem.get(stem)
        if not uri:
            return f'<p class="muted figure-missing">Figure <em>{_safe_html(stem)}</em> not yet generated. Run the analysis pipeline to produce it.</p>'
        title, caption = _FIGURE_META.get(stem, (stem.replace("outbreaker_", "").replace("_", " ").title(), ""))
        safe_title = _safe_html(title)
        safe_caption = _safe_html(caption)
        dl_name = f"{stem}.png"
        thumb_cls = "fig-thumb" if show_in_gallery else "fig-full"
        wide_cls = " fig-wide" if stem in {"outbreaker_resistance"} else ""
        return (
            f'<figure class="fig-card {thumb_cls}{wide_cls}" data-stem="{_safe_html(stem)}">'
            f'<div class="fig-img-wrap">'
            f'<img src="{uri}" alt="{safe_title}" loading="lazy" class="fig-img" '
            f'     onclick="openLightbox(\'{_safe_html(stem)}\')">'
            f'<button class="fig-zoom-btn" onclick="openLightbox(\'{_safe_html(stem)}\')" '
            f'        title="Click to zoom" aria-label="Zoom {safe_title}">&#x26F6;</button>'
            f'</div>'
            f'<figcaption>'
            f'<strong class="fig-title">{safe_title}</strong>'
            f'{"<p class=fig-caption>" + safe_caption + "</p>" if caption else ""}'
            f'<a class="fig-dl" href="{uri}" download="{_safe_html(dl_name)}" '
            f'   title="Download {safe_title}">&#x2B07; Download</a>'
            f'</figcaption>'
            f'</figure>'
        )

    # Build the full figures gallery (thumbnail grid at bottom of report)
    if figures_by_stem:
        graphics_html = [_figure_card(stem, show_in_gallery=True) for stem in sorted(figures_by_stem.keys())]
    else:
        graphics_html = []

    # -- Pair categorisation ----------------------------------------------------
    genomic_pairs = []
    model_only_pairs = []
    qc_resolution_pairs = []
    genomically_discordant = []
    for edge in sorted(high_confidence_edges, key=lambda x: float(x.get("probability") or 0.0), reverse=True)[:40]:
        src = str(edge.get("source") or "")
        tgt = str(edge.get("target") or "")
        prob = float(edge.get("probability") or 0.0)
        src_case = case_by_id.get(src) or {}
        tgt_case = case_by_id.get(tgt) or {}
        src_cluster = str(src_case.get("cluster_id") or "")
        tgt_cluster = str(tgt_case.get("cluster_id") or "")
        same_cluster = bool(src_cluster and src_cluster == tgt_cluster)
        pairwise_distance = pairwise_snp_matrix.get(_pair_key(src, tgt))
        src_qc = str(src_case.get("qc_status") or "not_reported")
        tgt_qc = str(tgt_case.get("qc_status") or "not_reported")
        qc_problem = (src_qc.lower() not in ("pass", "passed") or tgt_qc.lower() not in ("pass", "passed")
                      or bool(src_case.get("contamination_flag")) or bool(tgt_case.get("contamination_flag")))
        validation_flag = (
            "SNP-linked" if pairwise_distance is not None and pairwise_distance <= 12 and same_cluster and not qc_problem
            else ("QC-unresolved" if qc_problem
                  else ("D1: SNP>12" if pairwise_distance is not None and pairwise_distance > 12 else "Model-only"))
        )
        synthesis_pair = _synthesis_pair_for(src, tgt)
        synthesis_confidence = str(
            synthesis_pair.get("confidence")
            or synthesis_pair.get("confidence_code")
            or "Not synthesised"
        )
        synthesis_priority = synthesis_pair.get("priority_score")
        try:
            synthesis_priority_text = str(int(round(float(synthesis_priority))))
        except Exception:
            synthesis_priority_text = "n/a"
        synthesis_flags = synthesis_pair.get("flags") if isinstance(synthesis_pair.get("flags"), list) else []
        synthesis_flag_text = ", ".join(str(flag).replace("_", " ") for flag in synthesis_flags[:3]) or "none"
        concordance_bits = []
        if synthesis_pair.get("lineage_concordance"):
            concordance_bits.append(f"Lineage: {synthesis_pair.get('lineage_concordance')}")
        if synthesis_pair.get("resistance_profile_concordance"):
            concordance_bits.append(f"DR: {synthesis_pair.get('resistance_profile_concordance')}")
        record = {
            "pair": f"{_short_case_id(src)}\u2192{_short_case_id(tgt)}",
            "posterior": prob,
            "pairwise": str(pairwise_distance) if pairwise_distance is not None else "n/a",
            "qc": f"{src_qc}/{tgt_qc}",
            "validation_flag": validation_flag,
            "synthesis_confidence": synthesis_confidence,
            "synthesis_priority": synthesis_priority_text,
            "synthesis_concordance": "; ".join(concordance_bits) or "n/a",
            "synthesis_flags": synthesis_flag_text,
        }
        if qc_problem:
            qc_resolution_pairs.append(record)
        elif pairwise_distance is not None and pairwise_distance <= 12 and same_cluster:
            genomic_pairs.append(record)
        elif pairwise_distance is not None and pairwise_distance > 12:
            genomically_discordant.append(record)
        else:
            model_only_pairs.append(record)

    # -- Discordant pairs -------------------------------------------------------
    discordant_pairs = []
    for pair in sequence_pair_set.union(set(outbreaker_pair_prob.keys())):
        in_seq = pair in sequence_pair_set
        in_out = pair in outbreaker_pair_prob
        if in_seq == in_out:
            continue
        left, right = pair
        post = float(outbreaker_pair_prob.get(pair) or 0.0)
        pairwise_distance = pairwise_snp_matrix.get(pair)
        disc_code = "D1" if in_out and not in_seq and pairwise_distance is not None and int(pairwise_distance) > 12 else ("D2" if in_out and not in_seq else "D3")
        interp = ("Temporal support without pairwise SNP support" if in_out and not in_seq
                  else "Pairwise SNP support without outbreaker linkage")
        discordant_pairs.append({
            "pair": f"{_short_case_id(str(left))}-{_short_case_id(str(right))}",
            "pairwise": pairwise_distance,
            "posterior": post,
            "interpretation": interp,
            "disc_code": disc_code,
            "snp_result": "linked" if in_seq else "not_linked",
            "out_result": "linked" if in_out else "not_linked",
        })

    # -- Generate appendix CSVs from live data (no separate PDF run required) --
    _case_action_csv = [[
        "Case", "Cluster", "Pairwise SNP?", "NN SNP", "Likely link",
        "Posterior", "Tier", "QC", "Warning", "Recommended action",
    ]]
    for _row in case_rows[:30]:
        _cid = str(_row.get("case_id") or "")
        _clid = str(_row.get("cluster_id") or "")
        _inc = best_incoming.get(_cid)
        _out = best_outgoing.get(_cid)
        _nn_snp = nearest_neighbor_snp.get(_cid)
        if _inc and (not _out or _inc["probability"] >= _out["probability"]):
            _src = str(_inc["source"])
            _link = f"{_short_case_id(_src)} -> {_short_case_id(_cid)}"
            _post = _inc["probability"]
            _lp = pairwise_snp_matrix.get(_pair_key(_src, _cid))
            _same = bool((case_by_id.get(_src) or {}).get("cluster_id")) and str((case_by_id.get(_src) or {}).get("cluster_id") or "") == _clid
        elif _out:
            _tgt = str(_out["target"])
            _link = f"{_short_case_id(_cid)} -> {_short_case_id(_tgt)}"
            _post = _out["probability"]
            _lp = pairwise_snp_matrix.get(_pair_key(_cid, _tgt))
            _same = bool(_clid) and str((case_by_id.get(_tgt) or {}).get("cluster_id") or "") == _clid
        else:
            _link, _post, _lp, _same = "none", 0.0, None, bool(_clid)
        _qc = str(_row.get("qc_status") or "not_reported")
        _contam = bool(_row.get("contamination_flag"))
        _qc_prob = _qc.lower() not in ("pass", "passed") or _contam
        _pa = "yes" if _nn_snp is not None else "no"
        _tier = _confidence_tier(
            qc_status=_qc,
            contamination_flag=_contam,
            pairwise_distance=_lp if _lp is not None else _nn_snp,
            outbreaker_probability=_post,
            same_cluster=_same,
        )
        if _qc_prob:
            _warn = "Pairwise SNP required before transmission interpretation."
            _act = "Repeat/verify sequence before action."
        elif _lp is None:
            _warn = "Outbreaker-only link: requires genomic validation."
            _act = "Model-prioritised exposure review - confirm with pairwise SNP and epidemiology before action."
        elif _lp <= 12:
            _warn = "Pairwise SNP supports cluster linkage, but epidemiology must still corroborate the direction."
            _act = "Confirm with pairwise SNP and epidemiology before operational action."
        else:
            _warn = "Pairwise SNP distance is too high for direct transmission interpretation."
            _act = "Model-prioritised exposure review - confirm with pairwise SNP and epidemiology before action."
        _case_action_csv.append([
            _short_case_id(_cid),
            _short_case_id(_clid) if _clid else "none",
            _pa,
            str(_nn_snp) if _nn_snp is not None else "n/a",
            _link,
            f"{_post:.3f}" if _post else "n/a",
            _tier,
            f"{_qc}{' +contam' if _contam else ''}",
            _warn,
            _act,
        ])
    _write_csv_rows(_export_path("appendix_a_case_level_actions.csv"), _case_action_csv)

    _disc_csv = [["Case Pair", "Pairwise SNP result", "Outbreaker2", "Pairwise SNP", "Posterior", "Interpretation", "Code"]]
    for _dp in sorted(discordant_pairs, key=lambda x: x["posterior"], reverse=True):
        _disc_csv.append([
            _dp["pair"],
            _dp.get("snp_result", "n/a"),
            _dp.get("out_result", "n/a"),
            str(_dp["pairwise"]) if _dp["pairwise"] is not None else "n/a",
            f"{_dp['posterior']:.3f}" if _dp["posterior"] else "n/a",
            _dp["interpretation"],
            _dp.get("disc_code", ""),
        ])
    _write_csv_rows(_export_path("appendix_b_full_discordance_review.csv"), _disc_csv)

    # -- Action CSV rows (for load) ---------------------------------------------
    row_limit = None if full else 25
    action_rows_csv = _load_export_csv("appendix_a_case_level_actions.csv", limit=row_limit)
    discordance_rows_csv = _load_export_csv("appendix_b_full_discordance_review.csv", limit=row_limit)

    # -- Key nodes & edges from network JSON ------------------------------------
    key_nodes = (transmission_data or {}).get("key_nodes") or []
    network_edges = (transmission_data or {}).get("edges") or (transmission_data or {}).get("transmission_edges") or []

    # -- Report metadata --------------------------------------------------------
    report_label = "Full HTML" if full else "Short HTML"
    report_filename = "outbreaker_investigation_report_full.html" if full else "outbreaker_investigation_report.html"

    # -------------------------------------------------------------------------
    # Helper: render badge chip HTML
    # -------------------------------------------------------------------------
    def _badge(text: str) -> str:
        cls = {
            "SNP-linked": "badge-green",
            "Model-only": "badge-amber",
            "QC-unresolved": "badge-red",
            "D1: SNP>12": "badge-orange",
        }.get(text, "badge-grey")
        return f'<span class="badge {cls}">{_safe_html(text)}</span>'

    def _synthesis_badge(text: str) -> str:
        label = str(text or "Not synthesised")
        cls = {
            "Strong support": "badge-green",
            "strong_support": "badge-green",
            "Moderate support": "badge-amber",
            "moderate_support": "badge-amber",
            "Genomic-only signal": "badge-amber",
            "genomic_only_signal": "badge-amber",
            "Model-only signal": "badge-orange",
            "model_only_signal": "badge-orange",
            "Contradictory": "badge-red",
            "contradictory": "badge-red",
            "Insufficient evidence": "badge-grey",
            "insufficient_evidence": "badge-grey",
            "Not synthesised": "badge-grey",
        }.get(label, "badge-grey")
        return f'<span class="badge {cls}">{_safe_html(label.replace("_", " ").title() if "_" in label else label)}</span>'

    def _progress(value, label="") -> str:
        if value is None:
            return f'<span class="muted">n/a</span>'
        pct = min(100.0, max(0.0, float(value)))
        colour = "#2a9d8f" if pct >= 80 else ("#f4a261" if pct >= 60 else "#e63946")
        return (f'<div class="prog-wrap" title="{label}">'
                f'<div class="prog-bar" style="width:{pct:.1f}%;background:{colour}"></div>'
                f'<span class="prog-label">{pct:.1f}%</span></div>')

    def _metric_card(label: str, value: str, sub: str = "", alert: bool = False) -> str:
        cls = " metric-alert" if alert else ""
        return (f'<div class="metric{cls}"><div class="metric-label">{_safe_html(label)}</div>'
                f'<div class="metric-value">{_safe_html(value)}</div>'
                f'{"<div class=metric-sub>" + _safe_html(sub) + "</div>" if sub else ""}</div>')

    def _kv_rows_html(mapping, fields) -> str:
        if not isinstance(mapping, dict):
            return '<tr><td colspan="2" class="muted">No data available.</td></tr>'
        rows = ""
        for label, key in fields:
            v = mapping.get(key)
            if v is not None:
                rows += f"<tr><th>{_safe_html(label)}</th><td>{_safe_html(str(v))}</td></tr>"
        return rows or '<tr><td colspan="2" class="muted">No entries.</td></tr>'

    def _data_table_html(rows, columns, empty_msg="No data available.") -> str:
        if not rows:
            return f'<p class="muted">{_safe_html(empty_msg)}</p>'
        header = "".join(f"<th>{_safe_html(lbl)}</th>" for lbl, _ in columns)
        body = ""
        for row in rows:
            body += "<tr>" + "".join(f"<td>{_safe_html(str(row.get(k, '')))}</td>" for _, k in columns) + "</tr>"
        return f"<div class='tbl-wrap'><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>"

    def _pair_table_html(records, title, category_class="") -> str:
        if not records:
            return ""
        rows = ""
        for item in records:
            _post = f"{item['posterior']:.3f}"
            rows += (f"<tr><td class='mono'>{_safe_html(item['pair'])}</td>"
                     f"<td>{_safe_html(_post)}</td>"
                     f"<td>{_safe_html(item['pairwise'])}</td>"
                     f"<td>{_safe_html(item['qc'])}</td>"
                     f"<td>{_badge(item['validation_flag'])}</td>"
                     f"<td>{_synthesis_badge(item.get('synthesis_confidence'))}</td></tr>")
        return (f"<h4>{_safe_html(title)}</h4>"
                f"<div class='tbl-wrap'><table><thead><tr><th>Pair</th><th>Posterior</th><th>SNP dist</th>"
                f"<th>QC src/rec</th><th>Genomic flag</th><th>Synthesis confidence</th></tr></thead><tbody>{rows}</tbody></table></div>")

    # -------------------------------------------------------------------------
    # Computed summary values
    # -------------------------------------------------------------------------
    summary_total = int((kpi_data or {}).get("eligible_cases", total_cases))
    summary_sequenced = int((kpi_data or {}).get("sequenced_cases", len(sequence_by_case)))
    seq_pct = (kpi_data or {}).get("sequenced_pct")
    qc_pass_pct = (kpi_data or {}).get("qc_pass_pct")
    summary_coverage = f"{float(seq_pct):.1f}%" if seq_pct is not None else (
        f"{(summary_sequenced/summary_total)*100:.1f}%" if summary_total else "n/a")
    summary_qc_pass = f"{float(qc_pass_pct):.1f}%" if qc_pass_pct is not None else "n/a"
    extract_seq_pct = _pct(len(sequence_by_case), total_cases)
    extract_qc_pass_pct = _pct(qc_status_counts["pass"], len(sequence_by_case))
    extract_coverage_label = _pct_label(extract_seq_pct)
    extract_qc_pass_label = _pct_label(extract_qc_pass_pct)

    model_reliability = "Exploratory"
    if summary_data and summary_data.get("convergence_diagnostic") is not None:
        try:
            model_reliability = "Operationally stronger" if float(summary_data["convergence_diagnostic"]) <= 1.1 else "Exploratory"
        except Exception:
            model_reliability = "Exploratory"

    high_priority_open = sum(
        1 for r in cluster_action_rows
        if str(r.get("investigation_status") or "").lower() == "open" and int(r.get("priority_score") or 0) > 10
    )

    # -------------------------------------------------------------------------
    # CSS
    # -------------------------------------------------------------------------
    css = """
:root{
  --navy:#1d3557;--steel:#457b9d;--sky:#a8c8e1;--cloud:#eef4f9;
  --teal:#2a9d8f;--teal-bg:#e8f6f4;--alert:#e63946;--alert-bg:#fde8e8;
  --amber:#f4a261;--amber-bg:#fff4ec;--green:#16a34a;--green-bg:#f0fdf4;
  --ink:#1c2b3a;--muted:#5a7080;--rule:#c5d5e4;--bg:#f4f7fb;--card:#fff;
  --sidebar:260px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font-family:system-ui,Arial,sans-serif;line-height:1.5;display:flex;flex-direction:column;min-height:100vh}

/* -- Header -- */
header{background:linear-gradient(135deg,var(--navy) 0%,#254f78 100%);color:#fff;padding:1.6rem 2rem}
header h1{font-size:1.6rem;font-weight:700;letter-spacing:-.02em}
header p{color:#c8ddf0;font-size:.9rem;margin-top:.25rem}
.status-banner{display:inline-block;margin-top:.5rem;padding:.2rem .75rem;border-radius:20px;font-size:.78rem;font-weight:600;letter-spacing:.03em}
.status-draft{background:#e63946;color:#fff}
.status-review{background:#f4a261;color:#1c2b3a}
.status-ready{background:#16a34a;color:#fff}

/* -- Layout -- */
.layout{display:flex;flex:1;align-items:flex-start}

/* -- Sidebar TOC -- */
nav.sidebar{width:var(--sidebar);flex-shrink:0;position:sticky;top:0;max-height:100vh;overflow-y:auto;
  background:var(--navy);color:#c8ddf0;padding:1rem .75rem;font-size:.82rem;scrollbar-width:thin}
nav.sidebar h3{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:#7fa8c8;margin:.9rem 0 .3rem .2rem}
nav.sidebar a{display:block;padding:.28rem .5rem;border-radius:5px;color:#c8ddf0;text-decoration:none;transition:background .15s}
nav.sidebar a:hover,nav.sidebar a.active{background:rgba(255,255,255,.12);color:#fff}
nav.sidebar .sub{padding-left:1rem;font-size:.78rem}

/* -- Main content -- */
main{flex:1;min-width:0;padding:1.4rem 1.6rem 3rem;max-width:1100px}

/* -- Cards -- */
.card{background:var(--card);border:1px solid var(--rule);border-radius:12px;box-shadow:0 4px 14px rgba(21,38,64,.06);margin:1rem 0;padding:1.25rem 1.4rem;scroll-margin-top:1rem}
.card-note{background:var(--teal-bg);border-color:var(--teal)}
.card-warn{background:var(--amber-bg);border-color:var(--amber)}
.card-alert{background:var(--alert-bg);border-color:var(--alert)}

/* -- Section headings -- */
h2{font-size:1.18rem;font-weight:700;color:var(--navy);border-bottom:2px solid var(--rule);padding-bottom:.35rem;margin-bottom:.9rem}
h3{font-size:.98rem;font-weight:700;color:var(--steel);margin:1rem 0 .45rem}
h4{font-size:.88rem;font-weight:600;color:var(--muted);margin:.8rem 0 .35rem}

/* -- Metric dashboard grid -- */
.metrics-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:.7rem;margin:.75rem 0}
.metric{background:var(--cloud);border:1px solid var(--rule);border-radius:10px;padding:.8rem .9rem;position:relative;overflow:hidden}
.metric::before{content:'';position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--teal);border-radius:4px 0 0 4px}
.metric-alert::before{background:var(--alert)}
.metric-label{font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.metric-value{font-size:1.5rem;font-weight:700;color:var(--navy);line-height:1.2;margin:.15rem 0}
.metric-sub{font-size:.73rem;color:var(--muted)}

/* -- Progress bar -- */
.prog-wrap{position:relative;background:#e2eaf3;border-radius:20px;height:14px;overflow:hidden;min-width:80px}
.prog-bar{height:100%;border-radius:20px;transition:width .3s}
.prog-label{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.68rem;font-weight:600;color:var(--ink)}

/* -- Badges -- */
.badge{display:inline-block;padding:.15rem .5rem;border-radius:12px;font-size:.73rem;font-weight:600;white-space:nowrap}
.badge-green{background:var(--green-bg);color:var(--green);border:1px solid #86efac}
.badge-amber{background:var(--amber-bg);color:#c2410c;border:1px solid #fed7aa}
.badge-red{background:var(--alert-bg);color:var(--alert);border:1px solid #fca5a5}
.badge-orange{background:#fff7ed;color:#c2410c;border:1px solid #fdba74}
.badge-grey{background:#f1f5f9;color:#475569;border:1px solid #cbd5e1}

/* -- Tables -- */
.tbl-wrap{overflow-x:auto;margin:.5rem 0}
table{width:100%;border-collapse:collapse;font-size:.83rem;min-width:400px}
th,td{border:1px solid var(--rule);padding:.42rem .6rem;text-align:left;vertical-align:top}
th{background:#dde9f4;color:var(--navy);font-weight:600;font-size:.78rem;white-space:nowrap}
tbody tr:nth-child(even){background:var(--cloud)}
.kv-table th{width:38%;background:var(--cloud);font-weight:600;color:var(--steel)}
.mono{font-family:monospace;font-size:.78rem}

/* -- Collapsible details -- */
details{border:1px solid var(--rule);border-radius:8px;margin:.6rem 0;overflow:hidden}
details summary{padding:.65rem 1rem;background:var(--cloud);cursor:pointer;font-weight:600;color:var(--navy);font-size:.9rem;user-select:none;list-style:none}
details summary::before{content:'> ';font-size:.7rem;color:var(--steel)}
details[open] summary::before{content:'v '}
details > div{padding:.9rem 1rem}

/* -- Figures -- */
.figures-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:1.2rem;margin:.6rem 0}
.fig-card{border:1px solid var(--rule);border-radius:8px;background:#fff;overflow:hidden;display:flex;flex-direction:column;transition:box-shadow .2s}
.fig-card:hover{box-shadow:0 6px 20px rgba(21,38,64,.12)}
.fig-img-wrap{position:relative;background:#fff;overflow:auto;line-height:0}
.fig-img{width:100%;height:auto;display:block;cursor:zoom-in;transition:transform .25s}
.fig-card:hover .fig-img{transform:scale(1.01)}
.fig-zoom-btn{position:absolute;bottom:.4rem;right:.4rem;background:rgba(29,53,87,.82);color:#fff;border:none;border-radius:6px;padding:.3rem .45rem;font-size:.85rem;cursor:pointer;line-height:1;opacity:0;transition:opacity .2s}
.fig-card:hover .fig-zoom-btn{opacity:1}
.fig-card figcaption{padding:.65rem .75rem .7rem;flex:1;display:flex;flex-direction:column;gap:.25rem}
.fig-title{font-size:.83rem;color:var(--navy);display:block}
.fig-caption{font-size:.75rem;color:var(--muted);line-height:1.45;margin:0}
.fig-dl{font-size:.73rem;color:var(--teal);text-decoration:none;margin-top:auto;align-self:flex-start}
.fig-dl:hover{text-decoration:underline}
.figure-missing{font-style:italic;color:var(--muted);padding:.5rem 0}
.fig-inline{margin:.75rem 0}
.fig-full .fig-img-wrap{max-height:none;overflow:auto}
.fig-full .fig-img{object-fit:contain;max-height:720px;width:100%;padding:.35rem}
.fig-card.fig-wide .fig-img-wrap{overflow-x:auto;overflow-y:hidden}
.fig-card.fig-wide .fig-img{width:auto;max-width:none;min-width:100%;max-height:680px}
.fig-thumb .fig-img-wrap{height:240px}
.fig-thumb .fig-img{height:240px;object-fit:contain;padding:.35rem}

/* -- Lightbox -- */
dialog.lb{border:none;border-radius:14px;padding:0;max-width:96vw;max-height:96vh;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.55);background:#111}
dialog.lb::backdrop{background:rgba(0,0,0,.82)}
.lb-inner{position:relative;display:flex;flex-direction:column;max-height:96vh}
.lb-img{max-width:96vw;max-height:82vh;object-fit:contain;display:block;background:#111}
.lb-bar{background:rgba(0,0,0,.75);color:#f0f0f0;display:flex;align-items:center;gap:.75rem;padding:.5rem .8rem;font-size:.82rem}
.lb-caption{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lb-close{background:none;border:1px solid rgba(255,255,255,.35);color:#f0f0f0;border-radius:6px;padding:.25rem .65rem;cursor:pointer;font-size:.82rem}
.lb-close:hover{background:rgba(255,255,255,.15)}
.lb-dl{color:#80d4c8;font-size:.78rem;text-decoration:none;white-space:nowrap}
.lb-dl:hover{text-decoration:underline}
@media(max-width:640px){.figures-grid{grid-template-columns:1fr}}

/* -- Utilities -- */
.muted{color:var(--muted);font-size:.85rem}
pre{white-space:pre-wrap;background:#0f172a;color:#e2e8f0;border-radius:8px;padding:1rem;overflow:auto;font-size:.78rem}
.callout{background:var(--teal-bg);border-left:4px solid var(--teal);border-radius:0 8px 8px 0;padding:.7rem 1rem;margin:.5rem 0;font-size:.87rem}
.callout-warn{background:var(--amber-bg);border-left-color:var(--amber)}
.callout-alert{background:var(--alert-bg);border-left-color:var(--alert)}
.compact-list{margin:.45rem 0 0 1.1rem;padding:0}
.compact-list li{margin:.22rem 0}
.section-note{background:var(--cloud);border:1px solid var(--sky);border-radius:8px;padding:.65rem .9rem;margin:.5rem 0;font-size:.84rem;color:var(--ink)}
.tag{display:inline-flex;align-items:center;gap:.25rem;background:var(--cloud);border:1px solid var(--rule);border-radius:6px;padding:.1rem .45rem;font-size:.72rem;font-weight:600;color:var(--muted);margin:.1rem}

/* -- Print -- */
@media print{
  nav.sidebar{display:none}
  .layout{display:block}
  main{padding:.5rem;max-width:none}
  header{background:white;color:var(--ink);border-bottom:2px solid var(--rule);padding:.8rem 1rem}
  header p{color:var(--muted)}
  .card{box-shadow:none;page-break-inside:avoid;border:1px solid var(--rule)}
  details{border:1px solid var(--rule)}
  details summary{background:white}
  details > div{display:block !important}
  .prog-wrap{border:1px solid var(--rule)}
}

/* -- Responsive -- */
@media(max-width:780px){
  nav.sidebar{display:none}
  main{padding:1rem .75rem}
}
"""

    # -------------------------------------------------------------------------
    # Denominator counts
    # -------------------------------------------------------------------------
    denom_notified = int(total_cases)
    denom_sequenced = len(sequence_by_case)
    try:
        denom_culture_pos = db.execute(text("SELECT COUNT(*) FROM consensus_sequences")).scalar() or 0
    except Exception:
        db.rollback()
        denom_culture_pos = 0
    denom_qc_pass = int(qc_status_counts["pass"])
    _model_nodes: set = set()
    for _e in transmission_edges:
        if _e.get("source"):
            _model_nodes.add(str(_e["source"]))
        if _e.get("target"):
            _model_nodes.add(str(_e["target"]))
    denom_model_nodes = len(_model_nodes)
    model_node_note = (
        "Includes samples beyond current QC-pass set; model output is not operationally usable until QC is resolved"
        if denom_model_nodes > denom_qc_pass else
        "From transmission network JSON; 0 = analysis not yet run"
    )

    # -------------------------------------------------------------------------
    # Additional computed values for new sections
    # -------------------------------------------------------------------------

    # Population / denominator box HTML
    denom_html = f"""
<div class="tbl-wrap"><table><thead><tr>
  <th>Term</th><th>Definition</th><th>Count</th><th>Notes</th>
</tr></thead><tbody>
  <tr><td>Notified TB cases</td><td>Human cases in surveillance extract</td><td><strong>{_safe_html(str(denom_notified))}</strong></td><td>Source: cases table</td></tr>
  <tr><td>Culture-positive / sequencing-eligible</td><td>Cases with a consensus sequence loaded</td><td><strong>{_safe_html(str(denom_culture_pos))}</strong></td><td>Source: consensus_sequences</td></tr>
  <tr><td>Sequenced samples</td><td>Cases with sequence data in this extract</td><td><strong>{_safe_html(str(denom_sequenced))}</strong></td><td></td></tr>
  <tr><td>QC-pass genomes</td><td>Genomes passing QC - used for SNP clustering</td><td><strong>{_safe_html(str(denom_qc_pass))}</strong></td><td>Fail/contaminated excluded from inference</td></tr>
  <tr><td>outbreaker2 model nodes</td><td>Cases/samples included in transmission model</td><td><strong>{_safe_html(str(denom_model_nodes))}</strong></td><td>{_safe_html(model_node_note)}</td></tr>
</tbody></table></div>"""

    # QC thresholds box HTML
    qc_thresholds_html = """
<div class="tbl-wrap"><table><thead><tr><th>QC parameter</th><th>Pass threshold</th><th>Basis</th></tr></thead><tbody>
  <tr><td>Genome coverage breadth</td><td>&ge;95%</td><td>PHE TB WGS SOP / standard practice</td></tr>
  <tr><td>Mean depth</td><td>&ge;30&times;</td><td>Required for confident SNP calling</td></tr>
  <tr><td>Ambiguous bases (%)</td><td>&le;5%</td><td>High missingness distorts SNP distances</td></tr>
  <tr><td>Contamination</td><td>No mixed-lineage signal</td><td>Mixed lineage = likely contamination or co-infection - exclude pending investigation</td></tr>
  <tr><td>Minimum reads mapped</td><td>Platform-specific (see pipeline version)</td><td>Record in sequencing_runs table</td></tr>
  <tr><td>Exclusion rule</td><td>Any QC fail OR contamination flag = excluded from SNP clustering and outbreaker2</td><td>Conservative to avoid false transmission links</td></tr>
</tbody></table></div>
<div class="callout callout-warn" style="margin-top:.6rem">
  Samples with QC status <em>not reported</em> are treated as unresolved and excluded from cluster inference pending review.
  Thresholds above are defaults - site-specific SOP values override these if recorded in the pipeline provenance.
</div>"""

    # Epidemiological completeness table HTML - derive from case_rows
    epi_fields_check = [
        ("specimen_date",    "Specimen date"),
        ("geographic_region","Geographic region"),
        ("lineage",          "Lineage called"),
        ("predicted_drug_resistance", "Drug resistance called"),
        ("qc_status",        "QC status recorded"),
        ("cluster_id",       "Cluster assigned"),
    ]
    epi_total = len(case_rows) or 1
    epi_complete_html = "<div class='tbl-wrap'><table><thead><tr><th>Field</th><th>Populated</th><th>Missing</th><th>Completeness</th></tr></thead><tbody>"
    for db_key, label in epi_fields_check:
        populated = sum(1 for r in case_rows if r.get(db_key) not in (None, "", "null", "{}", "[]"))
        missing   = epi_total - populated
        pct       = (populated / epi_total) * 100
        alert_style = ' style="background:var(--alert-bg)"' if pct < 80 else ""
        epi_complete_html += (f"<tr{alert_style}><td>{_safe_html(label)}</td><td>{_safe_html(str(populated))}</td>"
                              f"<td>{_safe_html(str(missing))}</td><td>{_progress(pct)}</td></tr>")
    epi_complete_html += "</tbody></table></div>"
    epi_complete_html += """
<div class="callout callout-warn" style="margin-top:.6rem">
  <strong>Missing epi data domains</strong> (not directly capturable from genomic pipeline - require field data completion):<br>
  Demographics (age band, sex, country of birth, time in UK),
  Clinical infectiousness (pulmonary/extrapulmonary, smear status, cavitation, cough duration),
  Exposure setting (household, workplace, hostel, prison, healthcare, congregate setting),
  Contact tracing (named contacts, shared venues, tracing status),
  Vulnerability factors (homelessness, substance use, immunosuppression, migrant health, prison history),
  Timeline (symptom onset, diagnosis, isolation, treatment start, sequencing date).
  Complete these fields in the case management system for MDT review.
</div>"""

    synthesis_parameters = synthesis_data.get("parameters") if isinstance(synthesis_data, dict) else {}
    if not isinstance(synthesis_parameters, dict):
        synthesis_parameters = {}
    synthesis_method_rows = ""
    for label, key in [
        ("Synthesis low-SNP threshold", "low_snp_threshold"),
        ("Synthesis contradiction SNP threshold", "high_snp_contradiction_threshold"),
        ("Synthesis temporal window", "temporal_window_days"),
        ("Synthesis high posterior threshold", "high_posterior_threshold"),
    ]:
        if key in synthesis_parameters:
            synthesis_method_rows += (
                f"<tr><td>{_safe_html(label)}</td><td>Transmission synthesis</td>"
                f"<td>{_safe_html(str(synthesis_parameters.get(key)))}</td></tr>"
            )

    # Methods section HTML
    methods_html = f"""
<div class="tbl-wrap"><table><thead><tr><th>Pipeline component</th><th>Tool / approach</th><th>Version / parameter</th></tr></thead><tbody>
  <tr><td>Sequencing platform</td><td>{_safe_html(seq_platform or 'Not recorded - populate sequencing_runs.platform')}</td><td>{_safe_html(instrument or '-')}</td></tr>
  <tr><td>Library preparation</td><td>{_safe_html(library_prep or 'Not recorded - populate analysis_provenance.parameters')}</td><td>-</td></tr>
  <tr><td>Reference genome</td><td>{_safe_html(ref_genome or 'Not recorded - required')}</td><td>H37Rv recommended (NC_000962.3)</td></tr>
  <tr><td>Read mapping</td><td>{_safe_html(mapping_tool or 'Not recorded')}</td><td>-</td></tr>
  <tr><td>Variant calling</td><td>{_safe_html(variant_caller or 'Not recorded')}</td><td>Exclude PE/PPE and repetitive regions</td></tr>
  <tr><td>SNP clustering threshold</td><td>{_safe_html(snp_threshold or '12 SNPs (default)')}</td><td>NICE guideline / PHE SOP</td></tr>
  <tr><td>Resistance catalogue</td><td>{_safe_html(resist_cat or 'Not recorded - required')}</td><td>WHO/TBProfiler/Mykrobe</td></tr>
  <tr><td>Lineage-calling tool</td><td>{_safe_html(lineage_tool or 'Not recorded - required')}</td><td>-</td></tr>
  <tr><td>outbreaker2 version</td><td>{_safe_html(outbreaker_ver or 'Not recorded - required')}</td><td>-</td></tr>
  <tr><td>Generation time prior mean</td><td>Infectious to secondary case interval</td><td>{_safe_html(gen_time_mean or 'Not recorded')}</td></tr>
  <tr><td>Generation time prior SD</td><td></td><td>{_safe_html(gen_time_sd or 'Not recorded')}</td></tr>
    <tr><td>Sampling probability (pi)</td><td>Proportion of cases sampled</td><td>{_safe_html(sampling_prob or 'Not recorded')}</td></tr>
  <tr><td>Random seed</td><td>Required for reproducibility</td><td>{_safe_html(random_seed or 'Not recorded - required')}</td></tr>
  <tr><td>MCMC iterations</td><td></td><td>{_safe_html(str((summary_data or {{}}).get('n_iter', (summary_data or {{}}).get('n_generations', 'n/a'))))}</td></tr>
  <tr><td>Burn-in</td><td></td><td>{_safe_html(str((summary_data or {{}}).get('burnin', 'n/a')))}</td></tr>
  <tr><td>Posterior samples</td><td></td><td>{_safe_html(str((summary_data or {{}}).get('n_samples', 'n/a')))}</td></tr>
  {synthesis_method_rows}
</tbody></table></div>
<div class="callout callout-warn" style="margin-top:.5rem">
  Fields showing &ldquo;Not recorded&rdquo; must be populated in the <code>sequencing_runs</code>
  or <code>analysis_provenance</code> database tables before external circulation.
  Contact the bioinformatics lead to confirm the pipeline version and parameters used for this extract.
</div>"""

    # Transmission adjudication table - upgrade with final classification column
    def _adjudication_table(records, title):
        if not records:
            return ""
        rows = ""
        for item in records:
            post = float(item["posterior"])
            flag = item["validation_flag"]
            snp  = str(item["pairwise"])
            qc   = item["qc"]
            synth_conf = item.get("synthesis_confidence", "Not synthesised")
            synth_priority = item.get("synthesis_priority", "n/a")
            synth_concordance = item.get("synthesis_concordance", "n/a")
            synth_flags = item.get("synthesis_flags", "none")
            # Derive final classification
            if str(synth_conf).lower() in {"contradictory"}:
                final = "<span class='badge badge-red'>Do not escalate - synthesis contradictory</span>"
            elif str(synth_conf).lower() in {"strong support", "strong_support"}:
                final = "<span class='badge badge-green'>Synthesis supported - escalate with epi</span>"
            elif str(synth_conf).lower() in {"moderate support", "moderate_support"}:
                final = "<span class='badge badge-amber'>Synthesis moderate - MDT review</span>"
            elif flag == "SNP-linked":
                final = "<span class='badge badge-green'>Genomically supported - escalate with epi</span>"
            elif flag == "QC-unresolved":
                final = "<span class='badge badge-red'>Hold - repeat sequencing required</span>"
            elif flag == "D1: SNP>12":
                final = "<span class='badge badge-orange'>Do not escalate - SNP discordant</span>"
            else:
                final = "<span class='badge badge-amber'>Model hypothesis - epi corroboration required</span>"
            rows += (f"<tr><td class='mono'>{_safe_html(item['pair'])}</td>"
                     f"<td>{_safe_html(f'{post:.3f}')}</td>"
                     f"<td>{_safe_html(snp)}</td>"
                     f"<td>{_safe_html(qc)}</td>"
                     f"<td>{_badge(flag)}</td>"
                     f"<td>{_synthesis_badge(synth_conf)}</td>"
                     f"<td>{_safe_html(str(synth_priority))}</td>"
                     f"<td>{_safe_html(str(synth_concordance))}</td>"
                     f"<td>{_safe_html(str(synth_flags))}</td>"
                     f"<td><em class='muted'>Awaiting epi review</em></td>"
                     f"<td>{final}</td></tr>")
        return (f"<h4>{_safe_html(title)}</h4>"
                f"<div class='tbl-wrap'><table><thead><tr>"
                f"<th>Pair</th><th>Posterior</th><th>SNP dist</th>"
                f"<th>QC src/rec</th><th>Genomic flag</th><th>Synthesis confidence</th><th>Priority</th>"
                f"<th>Lineage/DR</th><th>Synthesis flags</th><th>Epidemiological link</th><th>Final classification</th>"
                f"</tr></thead><tbody>{rows}</tbody></table></div>")

    # PH interpretation statement
    snp_supported = len(genomic_pairs)
    model_only_ct = len(model_only_pairs)
    qc_unresolved_ct = len(qc_resolution_pairs)
    discordant_ct = len(genomically_discordant)

    if snp_supported > 0:
        ph_evidence_stmt = (
            f"There are <strong>{snp_supported}</strong> genomically-supported transmission pair(s) "
            f"(posterior >=0.70 and SNP distance <=12). These represent the highest-priority candidates "
            f"for operational action, but epidemiological corroboration is still required before field escalation."
        )
    else:
        ph_evidence_stmt = (
            "<strong>At present, there are no SNP-supported direct transmission links</strong> "
            "(no pairs meeting both posterior >=0.70 and SNP distance <=12 criteria). "
            "The outbreaker2 output identifies model-prioritised transmission hypotheses only."
        )

    repro_focus = (
        f"populating all {_safe_html(str(len(missing_repro)))} missing reproducibility field(s) before external circulation"
        if missing_repro else
        "confirming reproducibility metadata and appendices during information-governance review"
    )

    ph_interpretation_html = f"""
<div class="callout" style="font-size:.92rem;line-height:1.65">
  <p style="margin-bottom:.5rem">{ph_evidence_stmt}</p>
  <p style="margin-bottom:.5rem">
    There are <strong>{_safe_html(str(model_only_ct))}</strong> model-only link(s) (no pairwise SNP confirmation),
    <strong>{_safe_html(str(qc_unresolved_ct))}</strong> QC-unresolved pair(s) pending repeat sequencing, and
    <strong>{_safe_html(str(discordant_ct))}</strong> genomically discordant pair(s) (model-linked but SNP &gt;12).
  </p>
  <p><strong>Operational action should focus on:</strong>
    (1) resolving QC failures and contamination flags,
    (2) validating drug-resistance gene-drug mapping and confirming phenotypic DST,
    (3) completing epidemiological linkage data for {_safe_html(str(int(open_clusters)))} open cluster(s),
    (4) {repro_focus}{' - <strong>circulation is currently blocked</strong>' if missing_repro else ''}.
  </p>
  <p class="muted" style="margin-top:.4rem">This statement is automatically generated from available data.
  It must be reviewed and countersigned by the responsible public health physician before inclusion in any formal outbreak report.</p>
</div>"""

    # -------------------------------------------------------------------------
    # Build HTML sections
    # -------------------------------------------------------------------------

    # 1. Status banner
    _appendices_missing = not action_rows_csv or not discordance_rows_csv
    _tbp_import = (lineage_dr_data or {}).get("tbprofiler_db_import") or {}
    _mk_import = (lineage_dr_data or {}).get("mykrobe_db_import") or {}
    _dr_imported_rows = int((_tbp_import.get("imported_rows") or 0) + (_mk_import.get("imported_rows") or 0))
    _dr_skipped_rows = int((_tbp_import.get("skipped_rows") or 0) + (_mk_import.get("skipped_rows") or 0))
    _dr_unlinked = bool(lineage_dr_data) and _dr_skipped_rows > 0 and _dr_imported_rows == 0
    _dr_status = str((lineage_dr_data or {}).get("status") or "")
    _dr_tool_runs = [
        (lineage_dr_data or {}).get("tbprofiler_run") or {},
        (lineage_dr_data or {}).get("mykrobe_run") or {},
    ]
    _dr_any_attempted = any(int(run.get("attempted_samples") or 0) > 0 for run in _dr_tool_runs)
    _dr_skipped_or_blocked = bool(lineage_dr_data) and (
        _dr_status in {"ready_missing_inputs", "blocked_sample_id_mismatch", "blocked_no_tools", "blocked_tool_dependencies"}
        or (denom_sequenced > 0 and not _dr_any_attempted and _dr_imported_rows == 0)
    )
    _science_limitations = []
    if extract_qc_pass_pct is not None and extract_qc_pass_pct < 90:
        _science_limitations.append("QC below target")
    if model_reliability == "Exploratory":
        _science_limitations.append("model exploratory")
    if _dr_unlinked:
        _science_limitations.append("lineage/DR outputs unlinked")
    elif _dr_skipped_or_blocked:
        _science_limitations.append("lineage/DR validation unavailable")
    posterior_samples = 0
    if isinstance(summary_data, dict):
        try:
            posterior_samples = int(summary_data.get("n_samples") or 0)
        except Exception:
            posterior_samples = 0
    if posterior_samples < 100:
        model_reliability = "Exploratory"
    model_ready_blockers: list[str] = []
    if posterior_samples < 100:
        model_ready_blockers.append(
            f"only {posterior_samples} posterior sample(s) available after burn-in"
        )
    if extract_qc_pass_pct is not None and extract_qc_pass_pct < 90:
        model_ready_blockers.append(f"QC pass rate is {extract_qc_pass_label}, below the >=90% target")
    if qc_resolution_pairs:
        model_ready_blockers.append(f"{len(qc_resolution_pairs)} high-posterior pair(s) are QC-unresolved")
    if genomically_discordant:
        model_ready_blockers.append(f"{len(genomically_discordant)} high-posterior pair(s) are SNP-discordant")
    if not genomic_pairs:
        model_ready_blockers.append("no high-posterior pair is currently SNP-supported and QC-pass")
    model_operational_ready = not model_ready_blockers
    model_readiness_html = (
        '<div class="callout" style="margin-bottom:.6rem">'
        '<strong>Operational transmission inference: Ready for MDT review.</strong> '
        'High-posterior links include SNP-supported, QC-pass pairs; epidemiological corroboration is still required.'
        '</div>'
        if model_operational_ready else
        '<div class="callout callout-alert" style="margin-bottom:.6rem">'
        '<strong>Operational transmission inference: Not ready for field escalation.</strong> '
        'The report can support MDT review, but transmission directionality and model links must remain provisional. '
        '<ul class="compact-list">'
        + ''.join(f'<li>{_safe_html(item)}</li>' for item in model_ready_blockers)
        + '</ul></div>'
    )
    if missing_repro:
        status_html = f'<span class="status-banner status-draft">DRAFT - {len(missing_repro)} reproducibility field(s) missing</span>'
    elif _appendices_missing:
        status_html = '<span class="status-banner status-draft">INCOMPLETE &#8212; Appendices missing; not eligible for circulation</span>'
    elif _science_limitations:
        status_html = '<span class="status-banner status-review">ARTIFACT COMPLETE &#8212; governance/MDT review required</span>'
    else:
        status_html = '<span class="status-banner status-ready">Governance gate passed - eligible for circulation</span>'

    # QC and SNP warning callouts for executive summary
    _qc_warning_html = (
        f'<div class="callout callout-alert" style="margin-bottom:.6rem">'
        f'<strong>&#9888; Interpretation limited by QC failure rate.</strong> '
        f'QC pass rate is {extract_qc_pass_label} ({denom_qc_pass}/{denom_sequenced} sequenced), '
        f'below the &ge;90% target. Cluster assignments and model-based transmission links rely on '
        f'QC-pass genomes only. All conclusions are provisional pending QC resolution.</div>'
    ) if (extract_qc_pass_pct is not None and extract_qc_pass_pct < 90) else ""
    _dr_unlinked_warning_html = (
        f'<div class="callout callout-alert" style="margin-bottom:.6rem">'
        f'<strong>Lineage/DR outputs are not linked to active cases.</strong> '
        f'TBProfiler/Mykrobe generated outputs, but no rows were imported into '
        f'<code>tb_interpretation</code>. Treat lineage and resistance summaries as unavailable '
        f'until sample IDs are mapped to case IDs.</div>'
    ) if _dr_unlinked else ""
    _dr_skipped_warning_html = (
        f'<div class="callout callout-alert" style="margin-bottom:.6rem">'
        f'<strong>Lineage/DR validation did not run on the active FASTA.</strong> '
        f'Status: <code>{_safe_html(_dr_status or "unknown")}</code>. '
        f'Treat lineage and resistance summaries as unavailable until the pipeline is rerun '
        f'after <code>exports/dna.fasta</code> is generated and sample IDs validate against active cases.</div>'
    ) if (_dr_skipped_or_blocked and not _dr_unlinked) else ""
    _snp_warning_html = (
        f'<div class="callout callout-alert" style="margin-bottom:.6rem">'
        f'<strong>No SNP-supported direct transmission links identified.</strong> '
        f'Zero pairwise SNP links at &le;12 SNPs exist in this dataset. '
        f'All {high_confidence_all_count} high-posterior model edge(s) are '
        f'<em>exploratory only</em> and must not trigger field investigation without '
        f'SNP &le;12 and epidemiological corroboration.</div>'
    ) if pairwise_links_le_12 == 0 else ""
    outbreaker_output_assessment_html = f"""
<div class="section-note" style="margin-top:.7rem">
  <strong>Usefulness of current outbreaker2 outputs:</strong>
  The generated summary JSON, transmission network JSON, RDS object, diagnostic plots, tree, and derived pair table are useful for
  MDT review and audit. They are not yet sufficient for operational transmission inference because model inputs are not limited to
  QC-pass genomes and the current posterior sample count is {_safe_html(str(posterior_samples))}.
</div>
<div class="tbl-wrap"><table><thead><tr><th>Output area</th><th>Current state</th><th>Refinement needed</th></tr></thead><tbody>
  <tr><td>Model input set</td><td>{_safe_html(str(denom_model_nodes))} model node(s), {_safe_html(str(denom_qc_pass))} QC-pass genome(s)</td><td>Run outbreaker2 only on QC-pass, non-contaminated sequenced samples; export excluded cases with reasons.</td></tr>
  <tr><td>Posterior support</td><td>{_safe_html(str(high_confidence_all_count))} high-posterior model edge(s)</td><td>Label as hypotheses; add posterior support distribution and alternative ancestors per edge in reviewer tables.</td></tr>
  <tr><td>Diagnostics</td><td>{_safe_html(str(posterior_samples))} posterior sample(s) after burn-in</td><td>Increase retained posterior samples and report ESS/R-hat or equivalent multi-chain diagnostics before operational use.</td></tr>
  <tr><td>Review table</td><td>Pairs are classified against SNP/QC evidence in the report</td><td>Make this the primary operational artifact, with final MDT adjudication fields and exportable CSV.</td></tr>
</tbody></table></div>"""

    # 2. Dashboard cards
    _seq_sub = f"of {denom_notified} notified ({extract_coverage_label})"
    _qc_sub = f"of {denom_sequenced} sequenced ({extract_qc_pass_label})"
    dashboard_html = f"""
<div class="metrics-grid">
  {_metric_card("Notified cases", str(denom_notified), "Population denominator")}
  {_metric_card("Sequenced", str(denom_sequenced), _seq_sub, alert=extract_seq_pct is not None and extract_seq_pct < 80)}
  {_metric_card("QC-pass genomes", str(denom_qc_pass), _qc_sub, alert=extract_qc_pass_pct is not None and extract_qc_pass_pct < 90)}
  {_metric_card("QC unresolved", str(qc_status_counts['fail'] + qc_status_counts['not_reported'] + qc_status_counts['contamination']), f"{qc_status_counts['pass']} passed", alert=(qc_status_counts['fail'] + qc_status_counts['contamination']) > 0)}
  {_metric_card("Open clusters", str(open_clusters), f"{high_priority_open} priority >10")}
  {_metric_card("High-posterior model links", str(high_confidence_all_count), "Posterior >=0.70 - not confirmed transmission")}
  {_metric_card("SNP links <=12", str(pairwise_links_le_12), "Direct transmission candidates")}
  {_metric_card("Transmission inference", "Not ready" if not model_operational_ready else "MDT review", "Model output remains provisional" if not model_operational_ready else "Requires epi corroboration", alert=not model_operational_ready)}
</div>"""

    # Progress bars for coverage/QC
    seq_pct_bar = _progress(seq_pct, "Sequencing coverage %")
    qc_bar = _progress(qc_pass_pct, "QC pass rate %")
    _repro_status = '<span class="badge badge-red">Open</span>' if missing_repro else '<span class="badge badge-green">Complete</span>'
    _repro_action = (
        "Populate missing reproducibility metadata before external circulation"
        if missing_repro else
        "Confirm reproducibility metadata during information-governance review"
    )
    _repro_trigger = (
        f"Block all external distribution until {len(missing_repro)} required field(s) are populated"
        if missing_repro else
        "No metadata blocker currently detected; retain audit sign-off"
    )

    # 3. Top Actions Due Now table - with status, team, dates, escalation trigger
    top_actions_html = """
<div class="tbl-wrap"><table>
<thead><tr><th>#</th><th>Action</th><th>Responsible team</th><th>Due</th><th>Status</th><th>Date raised</th><th>Escalation trigger</th></tr></thead>
<tbody>
<tr><td>1</td><td>Repeat sequencing / QC review for all failed or contaminated samples</td><td>Laboratory / Bioinformatics</td><td>48 h</td><td><span class="badge badge-red">Open</span></td><td>{gen_at}</td><td>If repeat fails again: exclude from cluster; flag to MDT</td></tr>
<tr><td>2</td><td>Validate drug-resistance pipeline gene-drug mapping; suppress unusual mappings from operational reports</td><td>Bioinformatics / Microbiology</td><td>Immediate</td><td><span class="badge badge-red">Open</span></td><td>{gen_at}</td><td>If validation fails: quarantine resistance calls until pipeline fix confirmed</td></tr>
<tr><td>3</td><td>Confirm phenotypic DST for all genomic resistance signals before clinical use</td><td>TB Microbiology / MDT</td><td>Immediate</td><td><span class="badge badge-red">Open</span></td><td>{gen_at}</td><td>If DST unavailable: treat as MDR pending result; notify clinician</td></tr>
<tr><td>4</td><td>Complete epidemiological data for all open clusters (demographics, setting, contacts)</td><td>TB Nurses / HPT / PHA</td><td>Next MDT</td><td><span class="badge badge-amber">In progress</span></td><td>{gen_at}</td><td>If epi incomplete at MDT: defer cluster closure; document gap</td></tr>
<tr><td>5</td><td>Do not escalate model-only links to field investigation without SNP <=12 + epi corroboration</td><td>HPT / TB Nurses / MDT</td><td>Ongoing</td><td><span class="badge badge-amber">Standing</span></td><td>{gen_at}</td><td>If field escalation requested: require written MDT decision and documented epi rationale</td></tr>
<tr><td>6</td><td>{repro_action}</td><td>Bioinformatics / Lab Director</td><td>Before circulation</td><td>{repro_status}</td><td>{gen_at}</td><td>{repro_trigger}</td></tr>
<tr><td>7</td><td>MDT sign-off: document accepted/rejected/deferred for each open cluster</td><td>MDT Chair / PHA</td><td>Next MDT</td><td><span class="badge badge-amber">Pending</span></td><td>{gen_at}</td><td>If MDT not convened within 10 working days: escalate to programme lead</td></tr>
</tbody></table></div>""".format(
        gen_at=generated_at,
        repro_action=_safe_html(_repro_action),
        repro_status=_repro_status,
        repro_trigger=_safe_html(_repro_trigger),
    )

    # 4. MDT Governance table
    mdt_rows = [
        ("Circulation readiness", "Review required" if _science_limitations else ("Governance ready" if circulation_ok else "BLOCKED"), "; ".join(_science_limitations) if _science_limitations else "Complete mandatory reproducibility metadata before external circulation"),
        ("Transmission inference", "Not ready for field escalation" if not model_operational_ready else "Ready for MDT review", "Treat directionality as exploratory unless SNP <=12, QC pass, and epi corroboration are all present"),
        ("Open clusters", f"{int(open_clusters)} total / {high_priority_open} priority >10", "MDT review and epi data completion for all open clusters"),
        ("Discordant model links", f"{len(discordant_pairs)} identified", "Pairwise SNP + epi adjudication required"),
        ("MDT sign-off status", "Pending - MDT review required", "Chair to record: accepted / rejected / deferred for each open cluster"),
        ("Decision log", "Not yet completed", "Document MDT decisions in case management system; date-stamp and countersign"),
        ("QC failures", f"{qc_status_counts['fail']} fail / {qc_status_counts['contamination']} contamination", "Resolve before cluster assignment and model inference"),
    ]
    mdt_table_body = "".join(f"<tr><td>{_safe_html(a)}</td><td>{_safe_html(b)}</td><td>{_safe_html(c)}</td></tr>" for a, b, c in mdt_rows)
    mdt_html = f"""<div class="tbl-wrap"><table><thead><tr><th>Priority area</th><th>Current signal</th><th>Required MDT action</th></tr></thead>
<tbody>{mdt_table_body}</tbody></table></div>"""

    # 5. KPI table with progress bars
    kpi_kv = ""
    if isinstance(kpi_data, dict):
        kpi_fields = [
            ("Eligible cases (12 wks)", str(kpi_data.get("eligible_cases", "n/a"))),
            ("Sequenced cases", str(kpi_data.get("sequenced_cases", "n/a"))),
            ("Sequencing coverage", seq_pct_bar),
            ("QC reported cases", str(kpi_data.get("qc_reported_cases", "n/a"))),
            ("QC pass cases", str(kpi_data.get("qc_pass_cases", "n/a"))),
            ("QC fail cases", str(kpi_data.get("qc_fail_cases", "n/a"))),
            ("QC pass rate", qc_bar),
            ("Contamination flags", str(kpi_data.get("contamination_flag_cases", "n/a"))),
            ("Median days specimen->QC", str(kpi_data.get("median_days_specimen_to_qc", "n/a"))),
        ]
        if kpi_data.get("warning"):
            kpi_kv += f'<p class="muted">Warning: {_safe_html(str(kpi_data["warning"]))}</p>'
        kpi_kv += '<table class="kv-table"><tbody>' + "".join(
            f"<tr><th>{_safe_html(k)}</th><td>{v}</td></tr>" for k, v in kpi_fields
        ) + "</tbody></table>"

    # Regional representativeness
    region_rows_html = ""
    for row in ((kpi_data or {}).get("representativeness_by_region") or [])[:10]:
        region_rows_html += (f"<tr><td>{_safe_html(str(row.get('region','Unknown')))}</td>"
                             f"<td>{_safe_html(str(row.get('eligible_cases',0)))}</td>"
                             f"<td>{_safe_html(str(row.get('sequenced_cases',0)))}</td>"
                             f"<td>{_progress(row.get('sequenced_pct'))}</td></tr>")
    region_html = ""
    if region_rows_html:
        region_html = (f"<h3>Regional sequencing representativeness</h3>"
                       f"<div class='tbl-wrap'><table><thead><tr><th>Region</th><th>Eligible</th><th>Sequenced</th><th>Coverage</th></tr></thead>"
                       f"<tbody>{region_rows_html}</tbody></table></div>")

    kpi_extra_html = ""
    if isinstance(kpi_data, dict):
        lineage_rows = ""
        for row in (kpi_data.get("lineage_distribution") or [])[:6]:
            lineage_rows += (
                f"<tr><td>{_safe_html(str(row.get('lineage', 'unknown')))}</td>"
                f"<td>{_safe_html(str(row.get('case_count', 0)))}</td></tr>"
            )

        growth = kpi_data.get("cluster_growth") or {}
        growth_rows = (
            f"<tr><th>Cases in last 30 days</th><td>{_safe_html(str(growth.get('last_30_days', 0)))}</td></tr>"
            f"<tr><th>Cases in previous 30 days</th><td>{_safe_html(str(growth.get('previous_30_days', 0)))}</td></tr>"
            f"<tr><th>Cases in last 60 days</th><td>{_safe_html(str(growth.get('last_60_days', 0)))}</td></tr>"
            f"<tr><th>Cases in previous 60 days</th><td>{_safe_html(str(growth.get('previous_60_days', 0)))}</td></tr>"
            f"<tr><th>Cases in last 90 days</th><td>{_safe_html(str(growth.get('last_90_days', 0)))}</td></tr>"
            f"<tr><th>Cases in previous 90 days</th><td>{_safe_html(str(growth.get('previous_90_days', 0)))}</td></tr>"
        )

        seq_quality = (kpi_data.get("sequence_clustering_quality") or {})
        pairwise_sites = seq_quality.get("pairwise_comparable_sites") or {}
        link_sites = seq_quality.get("link_pair_comparable_sites") or {}
        seq_quality_rows = (
            f"<tr><th>Pairwise comparable sites (mean)</th><td>{_safe_html(str(pairwise_sites.get('mean', 'n/a')))}</td></tr>"
            f"<tr><th>Pairwise comparable sites (p10)</th><td>{_safe_html(str(pairwise_sites.get('p10', 'n/a')))}</td></tr>"
            f"<tr><th>Link-pair comparable sites (mean)</th><td>{_safe_html(str(link_sites.get('mean', 'n/a')))}</td></tr>"
            f"<tr><th>Link-pair comparable sites (p10)</th><td>{_safe_html(str(link_sites.get('p10', 'n/a')))}</td></tr>"
        )

        hist_rows = ""
        hist = seq_quality.get("pairwise_snp_distance_histogram") or sequence_summary_data.get("pairwise_snp_distance_histogram") or {}
        for bin_name in ["0-5", "6-12", "13-25", ">25", "unknown"]:
            if bin_name in hist:
                hist_rows += f"<tr><td>{_safe_html(bin_name)}</td><td>{_safe_html(str(hist.get(bin_name, 0)))}</td></tr>"

        lineage_html = ""
        if lineage_rows:
            lineage_html = (
                "<h3>Lineage distribution (reporting window)</h3>"
                "<div class='tbl-wrap'><table><thead><tr><th>Lineage</th><th>Cases</th></tr></thead>"
                f"<tbody>{lineage_rows}</tbody></table></div>"
            )
        hist_html = ""
        if hist_rows:
            hist_html = (
                "<h3>Pairwise SNP distance distribution</h3>"
                "<div class='tbl-wrap'><table><thead><tr><th>Distance bin</th><th>Pairs</th></tr></thead>"
                f"<tbody>{hist_rows}</tbody></table></div>"
            )

        kpi_extra_html = (
            "<h3>Growth and sequencing quality context</h3>"
            f"<table class='kv-table'><tbody>{growth_rows}</tbody></table>"
            "<h3>Comparable-site quality indicators</h3>"
            f"<table class='kv-table'><tbody>{seq_quality_rows}</tbody></table>"
            f"{lineage_html}"
            f"{hist_html}"
        )

    # 6. Weekly trends table
    trend_rows_html = ""
    for r in weekly_trends:
        trend_rows_html += (f"<tr><td>{_safe_html(str(r.get('week_start','')))}</td>"
                            f"<td>{_safe_html(str(r.get('eligible',r.get('eligible_cases',0))))}</td>"
                            f"<td>{_safe_html(str(r.get('sequenced',r.get('sequenced_cases',0))))}</td>"
                            f"<td>{_progress(r.get('seq_pct',r.get('sequenced_pct')))}</td>"
                            f"<td>{_progress(r.get('qc_pct',r.get('qc_pass_pct')))}</td></tr>")
    weekly_html = ""
    if trend_rows_html:
        weekly_html = (f"<div class='tbl-wrap'><table><thead><tr>"
                       f"<th>Week</th><th>Eligible</th><th>Sequenced</th><th>Coverage %</th><th>QC pass %</th>"
                       f"</tr></thead><tbody>{trend_rows_html}</tbody></table></div>")
    else:
        weekly_html = '<p class="muted">Weekly trend data unavailable.</p>'

    # 7. Analysis summary table
    analysis_keys = [
        ("Posterior samples", "n_samples"), ("MCMC iterations", "n_iter"), ("MCMC iterations", "n_generations"),
        ("Burn-in", "burnin"), ("Mean log-likelihood", "likelihood_mean"), ("Likelihood SD", "likelihood_sd"),
        ("Transmission probability", "transmission_probability"), ("Generation time mean (days)", "generation_time_mean"),
        ("Generation time SD (days)", "generation_time_sd"), ("Sampling probability", "sampling_probability"),
        ("Convergence diagnostic", "convergence_diagnostic"),
    ]
    analysis_html = ""
    if isinstance(summary_data, dict):
        rows_out = ""
        for label, key in analysis_keys:
            if key in summary_data:
                v = summary_data[key]
                fv = f"{float(v):.3f}" if isinstance(v, float) and abs(float(v)) < 10 else (f"{float(v):.2f}" if isinstance(v, float) else str(v))
                rows_out += f"<tr><th>{_safe_html(label)}</th><td>{_safe_html(fv)}</td></tr>"
        if rows_out:
            analysis_html = f"<table class='kv-table'><tbody>{rows_out}</tbody></table>"
    if not analysis_html:
        analysis_html = '<p class="muted">No outbreaker2 analysis artifact found.</p>'

    # 8. Cluster prioritisation table
    cluster_pri_html = ""
    if cluster_action_rows:
        cluster_pri_rows = "".join(
            f"<tr><td class='mono'>{_safe_html(str(r.get('cluster_id',''))[:12])}</td>"
            f"<td>{_safe_html(str(r.get('case_count',0)))}</td>"
            f"<td>{_safe_html(str(r.get('region_count',0)))}</td>"
            f"<td>{_safe_html(str(r.get('most_recent_specimen','n/a')))}</td>"
            f"<td>{_safe_html(str(r.get('investigation_status','unknown')))}</td>"
            f"<td><strong>{_safe_html(str(r.get('priority_score',0)))}</strong></td></tr>"
            for r in cluster_action_rows
        )
        cluster_pri_html = (f"<div class='tbl-wrap'><table><thead><tr>"
                            f"<th>Cluster</th><th>Cases</th><th>Regions</th><th>Most recent</th><th>Status</th><th>Priority score</th>"
                            f"</tr></thead><tbody>{cluster_pri_rows}</tbody></table></div>")
    else:
        cluster_pri_html = '<p class="muted">No cluster action data available.</p>'

    # 9. Lineage/DR summary
    interpreted = int(analysis_summary.get("interpreted_samples", 0) or 0)
    with_lineage = int(analysis_summary.get("samples_with_lineage", 0) or 0)
    with_resist = int(analysis_summary.get("samples_with_resistance_calls", 0) or 0)
    lin_cov = f"{(with_lineage/interpreted*100):.1f}%" if interpreted else "n/a"
    res_cov = f"{(with_resist/interpreted*100):.1f}%" if interpreted else "n/a"
    lin_kv_rows = [
        ("Interpreted samples", str(interpreted)),
        ("Samples with lineage", str(with_lineage)),
        ("Lineage coverage", lin_cov),
        ("Samples with resistance calls", str(with_resist)),
        ("Resistance coverage", res_cov),
        ("Any resistance signal", str(analysis_epi_summary.get("samples_with_any_resistance_signal", 0))),
        ("Rifampicin-resistant (suspected)", str(analysis_epi_summary.get("rifampicin_resistant_suspected", 0))),
        ("Isoniazid-resistant (suspected)", str(analysis_epi_summary.get("isoniazid_resistant_suspected", 0))),
        ("MDR (suspected)", str(analysis_epi_summary.get("mdr_suspected", 0))),
        ("FQ-resistant (suspected)", str(analysis_epi_summary.get("fluoroquinolone_resistant_suspected", 0))),
    ]
    lineage_table_html = "<table class='kv-table'><tbody>" + "".join(
        f"<tr><th>{_safe_html(k)}</th><td>{_safe_html(v)}</td></tr>" for k, v in lin_kv_rows
    ) + "</tbody></table>"
    if _dr_unlinked:
        lineage_table_html = (
            '<div class="callout callout-alert" style="margin-bottom:.6rem">'
            '<strong>Linked lineage/DR calls unavailable.</strong> Tool outputs exist, '
            'but sample IDs did not match active case IDs during import. Provide a sample ID map '
            'or regenerate tool inputs from the active case FASTA before using this section operationally.'
            '</div>'
            + lineage_table_html
        )
    elif _dr_skipped_or_blocked:
        lineage_table_html = (
            '<div class="callout callout-alert" style="margin-bottom:.6rem">'
            '<strong>Linked lineage/DR calls unavailable.</strong> Validation did not run successfully '
            'against the active FASTA. Rerun the full pipeline after outbreaker input export completes.'
            '</div>'
            + lineage_table_html
        )
    top_lineages = analysis_epi_summary.get("top_lineages") or []
    if top_lineages:
        lineage_table_html += "<p style='margin-top:.5rem'><strong>Top lineages: </strong>" + ", ".join(
            f"{_safe_html(x.get('lineage','?'))}: {_safe_html(str(x.get('count',0)))}" for x in top_lineages[:5]
        ) + "</p>"

    # 10. QC drill-down table
    qc_detail_rows = [r for r in case_rows if str(r.get("qc_status") or "").lower() not in ("pass", "passed") or bool(r.get("contamination_flag"))]
    if not qc_detail_rows:
        qc_detail_rows = [r for r in case_rows if r.get("qc_status")][:15]
    qc_detail_html = ""
    if qc_detail_rows:
        qc_trows = ""
        for r in qc_detail_rows[:30]:
            cov_raw = r.get("coverage_breadth")
            dep_raw = r.get("mean_depth")
            cov = f"{float(cov_raw):.1f}" if cov_raw is not None else "n/a"
            dep = f"{float(dep_raw):.1f}" if dep_raw is not None else "n/a"
            contam = "yes" if bool(r.get("contamination_flag")) else "no"
            qc_st = str(r.get("qc_status") or "not_reported")
            repeat = "yes" if qc_st.lower() not in ("pass", "passed") or contam == "yes" else "no"
            row_class = ' style="background:var(--alert-bg)"' if repeat == "yes" else ""
            action_badge = "<span class='badge badge-red'>Repeat</span>" if repeat == "yes" else "<span class='badge badge-green'>OK</span>"
            qc_trows += (f"<tr{row_class}><td class='mono'>{_safe_html(_short_case_id(str(r.get('case_id',''))))}</td>"
                         f"<td>{_safe_html(qc_st)}</td><td>{_safe_html(cov)}</td><td>{_safe_html(dep)}</td>"
                         f"<td>{_safe_html(contam)}</td>"
                         f"<td>{action_badge}</td></tr>")
        qc_detail_html = (f"<div class='tbl-wrap'><table><thead><tr><th>Sample</th><th>QC status</th><th>Coverage %</th>"
                          f"<th>Mean depth</th><th>Contamination</th><th>Action</th></tr></thead><tbody>{qc_trows}</tbody></table></div>")
    else:
        qc_detail_html = '<p class="muted">No QC details available.</p>'

    # QC summary cards
    qc_summary_html = f"""
<div class="metrics-grid">
  {_metric_card("QC pass", str(qc_status_counts['pass']), f"{extract_qc_pass_label} pass rate")}
  {_metric_card("QC fail", str(qc_status_counts['fail']), "Low coverage / threshold breach", alert=qc_status_counts['fail']>0)}
  {_metric_card("Contamination", str(qc_status_counts['contamination']), "Mixed signal - exclude pending repeat", alert=qc_status_counts['contamination']>0)}
  {_metric_card("Not reported", str(qc_status_counts['not_reported']), "QC metadata absent - treat as unresolved")}
  {_metric_card("Excluded from inference", str(excluded_from_outbreaker), "QC fail or contamination", alert=excluded_from_outbreaker>0)}
</div>"""

    # 11. Run-level QC table
    run_qc_html = ""
    if run_qc_rows_db:
        run_rows_out = ""
        for rl in run_qc_rows_db:
            total = int(rl.get("total_samples") or 0)
            fail = int(rl.get("fail_count") or 0)
            fail_pct = f"{round(100*fail/total,1)}%" if total else "n/a"
            alert_style = ' style="background:var(--alert-bg)"' if total and (fail/total) > 0.2 else ""
            run_rows_out += (f"<tr{alert_style}><td>{_safe_html(str(rl.get('run_date','n/a')))}</td>"
                             f"<td>{_safe_html(str(total))}</td><td>{_safe_html(str(int(rl.get('pass_count',0))))}</td>"
                             f"<td>{_safe_html(str(fail))}</td><td>{_safe_html(fail_pct)}</td>"
                             f"<td>{_safe_html(str(rl.get('mean_depth_avg','n/a')))}</td>"
                             f"<td>{_safe_html(str(rl.get('mean_coverage_avg','n/a')))}</td></tr>")
        run_qc_html = (f"<div class='tbl-wrap'><table><thead><tr><th>Run date (proxy)</th><th>Samples</th>"
                       f"<th>Pass</th><th>Fail</th><th>Fail %</th><th>Mean depth</th><th>Mean coverage %</th>"
                       f"</tr></thead><tbody>{run_rows_out}</tbody></table></div>"
                       "<p class='muted'>Rows highlighted red have fail rate &gt;20%. Assign run_id to enable full run-level audit.</p>")
    else:
        run_qc_html = '<p class="muted">Run-level QC unavailable - reported_at or run_id not recorded.</p>'

    # 12. Drug-resistance mutation table
    resistance_validation_lookup = _resistance_validation_lookup(resistance_validation_data)
    resistance_validation_summary = (resistance_validation_data or {}).get("summary") if isinstance(resistance_validation_data, dict) else {}
    if not isinstance(resistance_validation_summary, dict):
        resistance_validation_summary = {}
    dr_validation_summary_html = ""
    if resistance_validation_data:
        dr_validation_summary_html = (
            "<div class='callout callout-warn'>"
            "<strong>Local resistance validation artifact:</strong> "
            f"{_safe_html(str((resistance_validation_data or {}).get('status') or 'unknown'))}. "
            f"Mutation calls: {_safe_html(str(resistance_validation_summary.get('total_mutation_calls', 0)))}; "
            f"suppressed: {_safe_html(str(resistance_validation_summary.get('suppressed_calls', 0)))}; "
            f"validated: {_safe_html(str(resistance_validation_summary.get('validated_calls', 0)))}. "
            "Suppressed calls must not be reported operationally; not-validated calls require phenotype/catalogue review before clinical use."
            "</div>"
        )
    mut_rows_html = ""
    mut_count = 0
    suppressed_mutations = 0
    for base in mutation_rows_raw:
        case_id_mut = str(base.get("case_id") or "")
        for mut in _iter_resistance_mutations(base.get("resistance_mutations")):
            drug = str(mut.get("drug") or "n/a")
            gene = str(mut.get("gene") or "n/a")
            mutation = str(mut.get("mutation") or "n/a")
            validation_record = resistance_validation_lookup.get(
                _resistance_validation_key(case_id_mut, drug, gene, mutation)
            )
            validity = _drug_gene_status_label(drug, gene)
            report_status = _resistance_report_status_label(validation_record, drug, gene)
            if report_status == "Suppressed":
                suppressed_mutations += 1
                continue
            pred_text = _resistance_profile_text(base.get("predicted_drug_resistance"))
            badge_class = "badge-green" if "Valid" in validity else ("badge-red" if "Unusual" in validity else "badge-grey")
            report_badge_class = "badge-red" if report_status == "Suppressed" else ("badge-green" if report_status == "Validated" else "badge-amber")
            mut_rows_html += (f"<tr><td class='mono'>{_safe_html(_short_case_id(case_id_mut))}</td>"
                              f"<td>{_safe_html(drug)}</td><td class='mono'>{_safe_html(mutation)}</td>"
                              f"<td class='mono'>{_safe_html(gene)}</td>"
                              f"<td><span class='badge {badge_class}'>{_safe_html(validity)}</span></td>"
                              f"<td><span class='badge {report_badge_class}'>{_safe_html(report_status)}</span></td>"
                              f"<td>{_safe_html(str(mut.get('confidence','n/a')))}</td>"
                              f"<td>{_safe_html(pred_text)}</td></tr>")
            mut_count += 1
            if mut_count >= 60:
                break
        if mut_count >= 60:
            break
    dr_table_html = ""
    if mut_rows_html:
        dr_table_html = (f"{dr_validation_summary_html}<div class='tbl-wrap'><table><thead><tr><th>Case</th><th>Drug</th><th>Mutation</th>"
                         f"<th>Gene</th><th>Gene-drug status</th><th>Report status</th><th>Confidence</th><th>Predicted profile</th>"
                         f"</tr></thead><tbody>{mut_rows_html}</tbody></table></div>")
        if suppressed_mutations:
            dr_table_html += (
                f'<p class="muted" style="margin-top:.4rem">'
                f'{_safe_html(str(suppressed_mutations))} unusual gene-drug mapping(s) were suppressed from this operational table.'
                f'</p>'
            )
    else:
        dr_table_html = '<p class="muted">No reportable resistance-mutation details found.</p>'
        if suppressed_mutations:
            dr_table_html += (
                f'<p class="muted" style="margin-top:.4rem">'
                f'{_safe_html(str(suppressed_mutations))} unusual gene-drug mapping(s) were suppressed from this operational table.'
                f'</p>'
            )

    # 13. Cluster epidemiology tables
    cluster_epi_html = ""
    if cluster_epi_rows:
        cepi_rows = ""
        for r in cluster_epi_rows:
            cid = _short_case_id(str(r.get("cluster_id") or ""))
            lineage_dist = _lineage_distribution_text(str(r.get("cluster_id") or ""))
            tx_generations = _transmission_generation_text(str(r.get("cluster_id") or ""))
            rr_mdr = f"{int(r.get('rr_cases') or 0)}/{int(r.get('mdr_cases') or 0)}"
            recent = f"{int(r.get('recent_30d') or 0)}/{int(r.get('recent_60d') or 0)}/{int(r.get('recent_90d') or 0)}"
            cepi_rows += (f"<tr><td class='mono'>{_safe_html(cid)}</td><td>{_safe_html(str(r.get('cases',0)))}</td>"
                          f"<td>{_safe_html(str(r.get('first_specimen','n/a')))}</td><td>{_safe_html(str(r.get('latest_specimen','n/a')))}</td>"
                          f"<td>{_safe_html(str(r.get('median_snp_proxy','n/a')))}</td><td>{_safe_html(str(r.get('max_snp_proxy','n/a')))}</td>"
                          f"<td>{_safe_html(lineage_dist)}</td>"
                          f"<td>{_safe_html(tx_generations)}</td><td>{_safe_html(rr_mdr)}</td><td class='mono'>{_safe_html(_short_case_id(str(r.get('suspected_index_case','n/a'))))}</td>"
                          f"<td>{_safe_html(recent)}</td></tr>")
        cluster_epi_html = (f"<div class='tbl-wrap'><table><thead><tr><th>Cluster</th><th>Cases</th><th>First specimen</th>"
                                                        f"<th>Latest specimen</th><th>Median SNP</th><th>Max SNP</th><th>Lineage distribution</th><th>Sustained/max gen</th><th>RR/MDR cases</th><th>Index case</th><th>Recent 30/60/90d</th>"
                            f"</tr></thead><tbody>{cepi_rows}</tbody></table></div>")
        # Growth status
        growth_rows = ""
        for r in cluster_epi_rows:
            cid = _short_case_id(str(r.get("cluster_id") or ""))
            latest_raw = r.get("latest_specimen")
            if latest_raw is None:
                latest_str = "n/a"
                days_since = None
            elif hasattr(latest_raw, "isoformat"):
                latest_str = latest_raw.isoformat()[:10]
                try:
                    days_since = (today - (latest_raw.date() if hasattr(latest_raw, "date") else latest_raw)).days
                except Exception:
                    days_since = None
            else:
                latest_str = str(latest_raw)[:10]
                try:
                    days_since = (today - _dt.date.fromisoformat(latest_str)).days
                except Exception:
                    days_since = None
            if days_since is None:
                status = "Unknown"
                badge = "badge-grey"
            elif days_since < 90:
                status = "Active"
                badge = "badge-red"
            elif days_since < 180:
                status = "Slowing"
                badge = "badge-amber"
            else:
                status = "Likely inactive"
                badge = "badge-green"
            growth_rows += (f"<tr><td class='mono'>{_safe_html(cid)}</td><td>{_safe_html(str(r.get('cases',0)))}</td>"
                            f"<td>{_safe_html(latest_str)}</td><td>{_safe_html(str(r.get('recent_30d',0)))}</td>"
                            f"<td>{_safe_html(str(r.get('recent_60d',0)))}</td><td>{_safe_html(str(r.get('recent_90d',0)))}</td>"
                            f"<td><span class='badge {badge}'>{_safe_html(status)}</span></td></tr>")
        cluster_epi_html += (f"<h3>Cluster growth status</h3>"
                             f"<div class='tbl-wrap'><table><thead><tr><th>Cluster</th><th>Cases</th><th>Last case</th>"
                             f"<th>Cases 30d</th><th>Cases 60d</th><th>Cases 90d</th><th>Growth status</th>"
                             f"</tr></thead><tbody>{growth_rows}</tbody></table></div>"
                             "<p class='muted'>Active = last case &lt;90 days; Slowing = 90-180 days; Likely inactive = &gt;180 days. Formal closure requires MDT sign-off.</p>")
    else:
        cluster_epi_html = '<p class="muted">No cluster epidemiology data available.</p>'

    # 14. Method comparison
    comp_html = ""
    if isinstance(method_comparison_data, dict):
        coverage = method_comparison_data.get("coverage") or {}
        agreement = method_comparison_data.get("agreement") or {}
        comp_kv = [
            ("Sequence assigned cases", str(coverage.get("sequence_assigned_cases", 0))),
            ("Outbreaker assigned cases", str(coverage.get("outbreaker_assigned_cases", 0))),
            ("Overlap cases", str(coverage.get("overlap_cases", 0))),
            ("Pairwise precision", str(round(float(agreement.get("pairwise_precision_outbreaker_vs_sequence", 0.0)), 3))),
            ("Pairwise recall", str(round(float(agreement.get("pairwise_recall_outbreaker_vs_sequence", 0.0)), 3))),
            ("Pairwise Jaccard", str(round(float(agreement.get("pairwise_jaccard", 0.0)), 3))),
        ]
        comp_html = "<table class='kv-table'><tbody>" + "".join(f"<tr><th>{_safe_html(k)}</th><td>{_safe_html(v)}</td></tr>" for k, v in comp_kv) + "</tbody></table>"
    else:
        comp_html = '<p class="muted">No cluster method comparison artifact found.</p>'

    # 15. Sequence clustering snapshot
    seq_cluster_html = ""
    if isinstance(sequence_summary_data, dict):
        seq_kv = [(k.replace("_", " ").title(), str(sequence_summary_data[k])) for k in ["total_sequences", "assigned_sequences", "cluster_count", "largest_cluster_size", "singleton_count"] if k in sequence_summary_data]
        seq_cluster_html = "<table class='kv-table'><tbody>" + "".join(f"<tr><th>{_safe_html(k)}</th><td>{_safe_html(v)}</td></tr>" for k, v in seq_kv) + "</tbody></table>"
    else:
        seq_cluster_html = '<p class="muted">No sequence clustering summary artifact.</p>'

    # 16. Case-level actions (from CSV)
    action_limit_label = "all rows" if full else "first 25 rows"
    actions_csv_html = _data_table_html(
        action_rows_csv,
        [("Case", "Case"), ("Cluster", "Cluster"), ("Pairwise SNP?", "Pairwise SNP?"),
         ("NN SNP", "NN SNP"), ("Likely link", "Likely link"), ("Posterior", "Posterior"),
         ("Tier", "Tier"), ("QC", "QC"), ("Recommended action", "Recommended action")],
        "No case-level action data available. No cases with sequencing data were found for this outbreak."
    )
    discordance_csv_html = _data_table_html(
        discordance_rows_csv,
        [("Case pair", "Case Pair"), ("Pairwise SNP result", "Pairwise SNP result"),
         ("Outbreaker2", "Outbreaker2"), ("Pairwise SNP", "Pairwise SNP"),
         ("Posterior", "Posterior"), ("Code", "Code"), ("Interpretation", "Interpretation")],
        "No discordance review export found."
    )

    # Appendix A/B class and content (precomputed here since data is available)
    if action_rows_csv:
        appendix_a_class = 'card'
        appendix_a_body_html = f'<details open><summary>Table A1 \u2014 Case actions ({_safe_html(action_limit_label)})</summary><div>{actions_csv_html}</div></details>'
    else:
        appendix_a_class = 'card card-alert'
        appendix_a_body_html = '<div class="callout callout-alert"><strong>REPORT INCOMPLETE &#8212; Appendix A is missing.</strong> No case-level action data was generated. This appendix is required for MDT sign-off. The report cannot be circulated until case sequencing data is available. Ensure cases with sequencing results are loaded and re-generate this HTML report.</div>'
    if discordance_rows_csv:
        appendix_b_class = 'card'
        appendix_b_body_html = f'<details open><summary>Discordance table ({_safe_html(action_limit_label)})</summary><div>{discordance_csv_html}</div></details>'
    else:
        appendix_b_class = 'card card-alert'
        appendix_b_body_html = '<div class="callout callout-alert"><strong>REPORT INCOMPLETE &#8212; Appendix B is missing.</strong> No discordance data was generated. This may indicate insufficient sequencing data or no discordant model/SNP pairs in the current dataset. Ensure sequencing results are loaded and re-generate this HTML report.</div>'

    # By-cluster epi-completeness table (all domains default Red - field epi not in pipeline)
    if cluster_action_rows:
        _cluster_epi_rows = "".join(
            f"<tr><td class='mono'>{_safe_html(str(r.get('cluster_id', ''))[:12])}</td>"
            f"<td>{_safe_html(str(r.get('case_count', 0)))}</td>"
            f"<td><span class='badge badge-red'>Red &#8212; incomplete</span></td>"
            f"<td><span class='badge badge-red'>Red &#8212; incomplete</span></td>"
            f"<td><span class='badge badge-red'>Red &#8212; incomplete</span></td>"
            f"<td><span class='badge badge-red'>Red &#8212; incomplete</span></td>"
            f"<td><span class='badge badge-red'>Red &#8212; incomplete</span></td>"
            f"<td><span class='badge badge-red'>Blocked &#8212; epi incomplete</span></td></tr>"
            for r in cluster_action_rows
        )
        _cluster_epi_complete_html = (
            f"<div class='tbl-wrap'><table><thead><tr><th>Cluster</th><th>Cases</th>"
            f"<th>Demographics</th><th>Exposure setting</th><th>Contact tracing</th>"
            f"<th>Vulnerability factors</th><th>Timeline complete</th><th>MDT sign-off status</th>"
            f"</tr></thead><tbody>{_cluster_epi_rows}</tbody></table></div>"
            f'<div class="callout callout-alert" style="margin-top:.4rem"><strong>No cluster may be signed off at MDT until all Red fields are resolved.</strong>'
            f' Complete field epi data in the case management system before MDT review.</div>'
        )
    else:
        _cluster_epi_complete_html = '<p class="muted">No cluster data available for epi-completeness table.</p>'

    # 17. Transmission network section
    key_nodes_html = _data_table_html(
        key_nodes[:15 if not full else None],
        [("Case", "case_id"), ("Cluster", "cluster_id"), ("Region", "region"),
         ("Risk score", "risk_score"), ("Risk band", "risk_band"),
         ("Outgoing", "outgoing_links"), ("Incoming", "incoming_links")],
        "No priority-node data."
    )
    network_edges_html = _data_table_html(
        network_edges[:15 if not full else None],
        [("From", "source"), ("To", "target"), ("Posterior probability", "probability"),
         ("Posterior band", "confidence"), ("Inference", "inference")],
        "No transmission-link data."
    )
    network_meta_html = ""
    if isinstance(transmission_data, dict):
        net_kv = [(l, k) for l, k in [("Generated at", "generated_at"), ("Inference source", "inference_source"),
                   ("Provenance", "provenance"), ("Node count", "node_count"), ("Edge count", "edge_count"),
                   ("High-posterior model edges", "high_confidence_edges")] if transmission_data.get(k) is not None]
        network_meta_html = "<table class='kv-table'><tbody>" + "".join(
            f"<tr><th>{_safe_html(l)}</th><td>{_safe_html(str(transmission_data.get(k)))}</td></tr>" for l, k in net_kv
        ) + "</tbody></table>"

    # 18. Discordant pair summary table (from computed data)
    disc_computed_html = ""
    if discordant_pairs:
        def _fmt_disc(d):
            _pairwise_str = str(d['pairwise']) if d['pairwise'] is not None else 'n/a'
            _post_str = f"{d['posterior']:.3f}" if d['posterior'] else 'n/a'
            return (f"<tr><td class='mono'>{_safe_html(d['pair'])}</td>"
                    f"<td>{_safe_html(_pairwise_str)}</td>"
                    f"<td>{_safe_html(_post_str)}</td>"
                    f"<td><span class='badge badge-orange'>{_safe_html(d['disc_code'])}</span></td>"
                    f"<td>{_safe_html(d['interpretation'])}</td></tr>")
        disc_rows_out = "".join(
            _fmt_disc(d)
            for d in sorted(discordant_pairs, key=lambda x: x["posterior"], reverse=True)[:40 if full else 10]
        )
        disc_computed_html = (f"<div class='tbl-wrap'><table><thead><tr>"
                              f"<th>Pair</th><th>SNP dist</th><th>Posterior</th><th>Code</th><th>Interpretation</th>"
                              f"</tr></thead><tbody>{disc_rows_out}</tbody></table></div>"
                              "<p class='muted'>D1: outbreaker-linked, SNP&gt;12. D2: outbreaker-linked, SNP unavailable. D3: SNP-linked, no outbreaker direction.</p>")
    else:
        disc_computed_html = '<p class="muted">No discordant pairs identified from available outputs.</p>'

    # 19. Data provenance section - full extended table
    _missing_badge = "<span class='badge badge-red'>Missing &#8212; required</span>"
    _optional_badge = "<span class='badge badge-grey'>Not recorded</span>"

    def _prov_row(lbl, v, required=True):
        is_missing = v is None or (isinstance(v, str) and not v.strip())
        row_style = ' style="background:var(--alert-bg)"' if (is_missing and required) else ""
        cell = (_missing_badge if required else _optional_badge) if is_missing else _safe_html(str(v))
        return f"<tr{row_style}><th>{_safe_html(lbl)}</th><td>{cell}</td></tr>"

    prov_rows = "".join(_prov_row(lbl, v, required=True) for lbl, v in required_repro_metadata)
    # Extended optional fields
    extended_prov = [
        ("Sequencing platform",       seq_platform,   False),
        ("Instrument",                instrument,     False),
        ("Library prep method",       library_prep,   False),
        ("Mapping tool",              mapping_tool,   False),
        ("Variant caller",            variant_caller, False),
        ("SNP cluster threshold",     snp_threshold,  False),
        ("Generation time mean (d)",  gen_time_mean,  False),
        ("Generation time SD (d)",    gen_time_sd,    False),
        ("Sampling probability (pi)",  sampling_prob,  False),
    ]
    prov_rows += "".join(_prov_row(lbl, v, required=req) for lbl, v, req in extended_prov)
    prov_html = f"<table class='kv-table'><tbody>{prov_rows}</tbody></table>"

    # 20. Key concepts reference
    concepts = [
        ("Whole-Genome Sequencing (WGS)", "Reads the complete ~4.4 Mb genome of M. tuberculosis. More informative than conventional typing (MIRU, spoligotyping)."),
        ("SNP", "A single base-pair difference. Closely related strains share few SNPs. Used as a genetic distance metric."),
        ("SNP threshold for transmission", f"<={low_snp_threshold} SNPs: potentially linked under this run configuration. <=5 SNPs: recent direct transmission likely. >={high_snp_contradiction_threshold} SNPs: synthesis contradiction threshold for recent direct transmission."),
        ("Lineage", "M. tuberculosis classified into 7+ major lineages (L1-L7). Influences drug-resistance patterns and transmissibility."),
        ("Cluster", "Cases genetically similar within the SNP threshold. Does not prove direct transmission - epidemiological linkage required to confirm routes."),
        ("outbreaker2", "Bayesian MCMC method combining SNP distances with collection dates to probabilistically infer who-infected-whom. Posterior probabilities are hypotheses, not proofs."),
        ("MCMC convergence", "Convergence diagnostic near 1.0 = reliable. Values >1.1 = interpret cautiously."),
        ("Drug resistance", "Genomic mutations predict resistance. MDR-TB = resistant to isoniazid + rifampicin. XDR-TB = additional resistance. Genomic DR requires phenotypic DST confirmation."),
    ]
    concepts_html = "".join(
        f"<details><summary>{_safe_html(term)}</summary><div><p>{_safe_html(defn)}</p></div></details>"
        for term, defn in concepts
    )

    # 21. Raw artifacts section (full only)
    raw_artifacts_html = ""
    if full:
        raw_artifacts_html = f"""
<div class="card" id="raw-artifacts">
  <h2>Full machine-readable artifacts</h2>
  <p class="muted">Complete JSON exports used to produce this report.</p>
  <details><summary>Outbreaker2 summary artifact</summary><div><pre>{_safe_html(json.dumps(summary_data, indent=2, default=str) if summary_data else 'No artifact found.')}</pre></div></details>
  <details><summary>Transmission network artifact</summary><div><pre>{_safe_html(json.dumps(transmission_data, indent=2, default=str) if transmission_data else 'No artifact found.')}</pre></div></details>
  <details><summary>Sequence clustering summary</summary><div><pre>{_safe_html(json.dumps(sequence_summary_data, indent=2, default=str) if sequence_summary_data else 'No artifact found.')}</pre></div></details>
  <details><summary>Lineage/DR validation</summary><div><pre>{_safe_html(json.dumps(lineage_dr_data, indent=2, default=str) if lineage_dr_data else 'No artifact found.')}</pre></div></details>
  <details><summary>Resistance validation</summary><div><pre>{_safe_html(json.dumps(resistance_validation_data, indent=2, default=str) if resistance_validation_data else 'No artifact found.')}</pre></div></details>
</div>"""

    appendix_sections_html = (
        f"""
    <!-- =================================================================== -->
    <!-- APPENDICES -->
    <!-- =================================================================== -->
    <section class="{appendix_a_class}" id="appendix-a">
      <h2>Appendix A &#8212; Case-level operational actions</h2>
      {appendix_a_body_html}
    </section>

    <section class="{appendix_b_class}" id="appendix-b">
      <h2>Appendix B &#8212; Full discordance review</h2>
      {appendix_b_body_html}
    </section>
"""
        if full else ""
    )
    figures_section_html = (
        f"""
    <!-- FIGURES -->
    <section class="card" id="figures">
      <h2>Figures overview</h2>
      <p class="muted" style="margin-bottom:.7rem">All generated figures. Click any image to zoom; use the download link to save. Figures are also embedded inline within their relevant report sections above.</p>
      <div class="figures-grid">{''.join(graphics_html) if graphics_html else '<p class="muted">No outbreak graphics found in exports/. Run the analysis pipeline to generate figures.</p>'}</div>
    </section>

    <!-- LIGHTBOX DIALOG -->
    <dialog class="lb" id="lightbox" aria-modal="true" aria-label="Figure zoom view">
      <div class="lb-inner">
        <img class="lb-img" id="lb-img" src="" alt="">
        <div class="lb-bar">
          <span class="lb-caption" id="lb-caption"></span>
          <a class="lb-dl" id="lb-dl" href="" download="">&#x2B07; Download</a>
          <button class="lb-close" onclick="document.getElementById('lightbox').close()" aria-label="Close zoom">&#x2715; Close</button>
        </div>
      </div>
    </dialog>
"""
        if full else ""
    )
    concepts_section_html = (
        f"""
    <!-- KEY CONCEPTS -->
    <section class="card" id="concepts">
      <h2>Key concepts (Appendix D)</h2>
      <p class="muted" style="margin-bottom:.5rem">Click to expand each definition.</p>
      {concepts_html}
    </section>
"""
        if full else ""
    )

    # -------------------------------------------------------------------------
    # Assemble final HTML
    # -------------------------------------------------------------------------

    # Build the improved pairs section using adjudication table
    adj_genomic_html    = _adjudication_table(genomic_pairs[:20 if not full else None],    f"Genomically supported (SNP <={low_snp_threshold}, shared cluster)")
    adj_model_html      = _adjudication_table(model_only_pairs[:20 if not full else None],  "Model-only - no pairwise SNP data")
    adj_discordant_html = _adjudication_table(genomically_discordant[:20 if not full else None], f"Genomically discordant (posterior >={high_posterior_label}, SNP >{low_snp_threshold})")
    adj_qcunres_html    = _adjudication_table(qc_resolution_pairs[:20 if not full else None], "QC-unresolved - hold pending repeat sequencing")

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Outbreak Investigation Report - NI TB Genomic Surveillance</title>
  <style>{css}</style>
</head>
<body>
<header>
  <h1>NI TB Genomic Surveillance</h1>
  <p>Outbreak Investigation Report &mdash; generated {_safe_html(generated_at)} &nbsp;&middot;&nbsp; {_safe_html(report_label)}</p>
  {status_html}
</header>

<div class="layout">
  <!-- -- Sticky sidebar nav -- -->
  <nav class="sidebar" aria-label="Report sections">
    <h3>Overview</h3>
    <a href="#executive">Executive summary</a>
    <a href="#actions-now">Top actions due now</a>
    <a href="#mdt">MDT governance</a>
    <a href="#about">About this report</a>
    <a href="#denominators">Denominators</a>
    <h3>Analysis</h3>
    <a href="#analysis">outbreaker2 analysis</a>
    <a href="#transmission">Transmission network</a>
    <a href="#pairs">High-posterior model hypotheses</a>
    <a href="#snp-summary">Pairwise SNP summary</a>
    <a href="#interpretation">Outbreak interpretation</a>
    <h3>Sequencing &amp; QC</h3>
    <a href="#kpis">Programme KPIs</a>
    <a href="#weekly">Weekly trends</a>
    <a href="#qc">QC drill-down</a>
    <a href="#run-qc">Run-level QC</a>
    <h3>Resistance</h3>
    <a href="#lineage">Lineage/DR summary</a>
    <a href="#mutations">Mutation details</a>
    <h3>Clusters</h3>
    <a href="#clusters">Cluster epidemiology</a>
    <a href="#cluster-pri">Cluster prioritisation</a>
    <h3>Methods</h3>
    <a href="#methods">Methods &amp; pipeline</a>
    <a href="#provenance">Data provenance</a>
    <a href="#epi-completeness">Epi data completeness</a>
    <h3>Appendices</h3>
    {'<a href="#appendix-a">Appendix A - Case actions</a><a href="#appendix-b">Appendix B - Discordance</a><a href="#figures">Figures</a><a href="#concepts">Key concepts</a>' if full else '<a href="#interpretation">Current interpretation</a>'}
    {'<a href="#raw-artifacts">Raw artifacts</a>' if full else ''}
  </nav>

  <main>
    <!-- =================================================================== -->
    <!-- EXECUTIVE SUMMARY -->
    <!-- =================================================================== -->
    <section class="card" id="executive">
      <h2>Executive summary</h2>
      {dashboard_html}
      {model_readiness_html}
      {analysis_quality_warnings_html}
      {confidence_gate_table_html}
      {_qc_warning_html}{_snp_warning_html}{_dr_unlinked_warning_html}{_dr_skipped_warning_html}{'<div class="callout callout-alert"><strong>DRAFT REPORT:</strong> Missing reproducibility fields: ' + _safe_html(', '.join(missing_repro)) + '. External circulation is blocked until these are populated.</div>' if missing_repro else '<div class="callout"><strong>Artifact completeness gate passed.</strong> Reproducibility metadata and appendices are present. Operational use still requires MDT, QC, and information-governance review.</div>'}
    </section>

    <!-- TOP ACTIONS -->
    <section class="card" id="actions-now">
      <h2>Top actions due now</h2>
      <div class="section-note">Items marked <strong>model-only</strong> or <strong>exploratory</strong> require genomic validation and epidemiological corroboration before operational action.</div>
      {top_actions_html}
    </section>

    <!-- MDT GOVERNANCE -->
    <section class="card" id="mdt">
      <h2>MDT governance summary</h2>
      {mdt_html}
      <p class="muted" style="margin-top:.5rem">Full MDT action sheet with final discordant-pair counts is in Appendix B.</p>
    </section>

    <!-- ABOUT -->
    <section class="card" id="about">
      <h2>About this report</h2>
      <p style="font-size:.87rem">This report is produced by the Northern Ireland TB Genomic Surveillance platform using whole-genome sequencing (WGS) data and epidemiological case records. It supports TB programme staff and public health investigators by providing genomic evidence for transmission clusters, drug-resistance profiles, and programme performance metrics.</p>
      <div class="callout-warn callout" style="margin-top:.6rem"><strong>Decision-support tool only.</strong> All findings must be reviewed and acted on by a qualified clinician or public health professional. No automated decisions are made.</div>
            {mock_outbreaker_html}
            {synthesis_version_warning_html}
    </section>

    <!-- POPULATION AND DENOMINATORS -->
    <section class="card" id="denominators">
      <h2>Population and denominators</h2>
      <div class="section-note">Use this table to interpret all percentages in this report. Every metric is expressed relative to one of these denominator counts.</div>
      {denom_html}
    </section>

    <!-- =================================================================== -->
    <!-- ANALYSIS -->
    <!-- =================================================================== -->
    <section class="card" id="analysis">
      <h2>outbreaker2 analysis summary</h2>
      {analysis_html}
      {model_readiness_html}
      {outbreaker_output_assessment_html}
      {analysis_quality_warnings_html}
      <div class="section-note" style="margin-top:.7rem">
        <strong>How to interpret:</strong> Pairs with high posterior transmission probability are model-prioritised hypotheses only.
                They should not be interpreted as direct transmission unless supported by pairwise SNP distance &lt;={low_snp_threshold}, QC pass status, and epidemiological corroboration.
      </div>
      <h3>MCMC diagnostics</h3>
      <div class="figures-grid">
        {_figure_card("outbreaker_trace")}
        {_figure_card("outbreaker_hist")}
      </div>
    </section>

    <!-- TRANSMISSION NETWORK -->
    <section class="card" id="transmission">
      <h2>Transmission network</h2>
      {network_meta_html}
      <div class="fig-inline">{_figure_card("outbreaker_tree")}</div>
      <h3>Model-prioritised nodes</h3>
      {key_nodes_html}
      <h3>High-posterior model edges</h3>
      {network_edges_html}
    </section>

    <!-- MODEL-PRIORITISED PAIRS - WITH ADJUDICATION TABLE -->
    <section class="card" id="pairs">
      <h2>High-posterior model hypotheses</h2>
      <div class="callout callout-alert">
        <strong>Do not treat these model edges as confirmed transmission.</strong>
                Posterior probability &ge;{high_posterior_label} is a model signal only; operational escalation requires QC-pass sequence data,
                SNP &le;{low_snp_threshold} support, and epidemiological corroboration.
      </div>
      <div style="margin:.6rem 0">
        <span class="tag">{_safe_html(str(len(genomic_pairs)))} SNP-linked</span>
        <span class="tag">{_safe_html(str(len(model_only_pairs)))} Model-only</span>
        <span class="tag">{_safe_html(str(len(genomically_discordant)))} D1: SNP&gt;12</span>
        <span class="tag">{_safe_html(str(len(qc_resolution_pairs)))} QC-unresolved</span>
      </div>
      <div class="section-note">The <strong>Epidemiological link</strong> and <strong>Final classification</strong> columns below are pre-populated with default values. The MDT should review each pair and record the final adjudication decision in the case management system before external circulation.</div>
      {adj_genomic_html}
      {adj_model_html}
      {adj_discordant_html}
      {adj_qcunres_html}
            {('<p class="muted">No high-posterior (&ge;' + _safe_html(high_posterior_label) + ') model edges found in transmission network.</p>' if not high_confidence_edges else '')}
    </section>

    <!-- SNP SUMMARY -->
    <section class="card" id="snp-summary">
      <h2>Pairwise SNP distance summary</h2>
      <div class="section-note">
                &le;{low_snp_threshold} SNPs = operational low-SNP support threshold for this run.
                &gt;{low_snp_threshold} SNPs = direct transmission less plausible and requires adjudication.
        SNP unavailable = repeat sequencing required before inference.
      </div>
      <div class="tbl-wrap"><table><thead><tr><th>SNP distance category</th><th>Pairs</th><th>Operational implication</th></tr></thead><tbody>
        <tr><td>0-5 SNPs (direct)</td><td>{_safe_html(str(sum(1 for d in pairwise_snp_matrix.values() if d<=5)))}</td><td>Immediate: probable direct transmission - contact trace; confirm epi link</td></tr>
        <tr><td>6-12 SNPs (probable)</td><td>{_safe_html(str(sum(1 for d in pairwise_snp_matrix.values() if 6<=d<=12)))}</td><td>Priority: probable cluster; review shared setting and exposures</td></tr>
        <tr><td>13-25 SNPs (possible shared source)</td><td>{_safe_html(str(sum(1 for d in pairwise_snp_matrix.values() if 13<=d<=25)))}</td><td>Review: possible shared source/reactivation; epi adjudication required</td></tr>
        <tr><td>&gt;25 SNPs (unlikely direct)</td><td>{_safe_html(str(sum(1 for d in pairwise_snp_matrix.values() if d>25)))}</td><td>Low priority: unlikely direct recent transmission; monitor only</td></tr>
        <tr><td>SNP unavailable</td><td>{_safe_html(str(sum(1 for r in case_rows if not sequence_by_case.get(str(r.get('case_id',''))) and r.get('case_id'))))}</td><td>Hold: repeat sequencing or QC resolution required before inference</td></tr>
      </tbody></table></div>
    </section>

    <!-- OUTBREAK INTERPRETATION -->
    <section class="card" id="interpretation">
      <h2>Current outbreak interpretation</h2>
      {ph_interpretation_html}

      <h3>Counts and denominators in this report</h3>
      <div class="section-note">Definitions match the denominator box above. All model-prioritised links are hypotheses only - zero SNP-supported links means no validated direct transmission candidates at this time.</div>
      <div class="tbl-wrap"><table><thead><tr><th>Metric</th><th>Count</th><th>Definition</th></tr></thead><tbody>
        <tr><td>High-posterior model links &ge;{_safe_html(high_posterior_label)}</td><td>{_safe_html(str(high_confidence_all_count))}</td><td>All outbreaker2 edges with posterior probability &ge;{_safe_html(high_posterior_label)}; these are hypotheses, not confirmed transmission links</td></tr>
        <tr><td>Discordant pairs reviewed</td><td>{_safe_html(str(len(discordant_pairs)))}</td><td>All model-linked pairs showing SNP/model discordance requiring adjudication</td></tr>
        <tr><td>Displayed network links</td><td>{_safe_html(str(high_confidence_snapshot_count))}</td><td>Links in network JSON snapshot (may be filtered for display)</td></tr>
      </tbody></table></div>

      <h3>Discordant pairs</h3>
      {disc_computed_html}
    </section>

    <!-- =================================================================== -->
    <!-- KPIs & TRENDS -->
    <!-- =================================================================== -->
    <section class="card" id="kpis">
      <h2>Programme surveillance KPIs (last 12 weeks)</h2>
      {kpi_kv}
      {region_html}
            {kpi_extra_html}
    </section>

    <section class="card" id="weekly">
      <h2>Weekly surveillance trends (12 weeks)</h2>
      {weekly_html}
    </section>

    <!-- =================================================================== -->
    <!-- QC -->
    <!-- =================================================================== -->
    <section class="card" id="qc">
      <h2>QC failure drill-down</h2>
      {qc_summary_html}
      <h3>QC pass/fail thresholds applied</h3>
      {qc_thresholds_html}
      <h3>Sample-level QC detail</h3>
      <div class="section-note" style="margin-top:.6rem">
        Samples highlighted red require repeat sequencing or resolution before operational inference.
        Contaminated samples must be excluded from cluster assignment pending repeat.
        The <strong>qc_failure_reason</strong> field (from sample_qc_metrics) is shown where available.
      </div>
      {qc_detail_html}
    </section>

    <section class="card" id="run-qc">
      <h2>Run-level QC summary</h2>
      <div class="section-note">Runs with fail rate &gt;20% or mean coverage &lt;95% should be reviewed before results are used for cluster assignment.</div>
      {run_qc_html}
    </section>

    <!-- =================================================================== -->
    <!-- LINEAGE / DR -->
    <!-- =================================================================== -->
    <section class="card" id="lineage">
      <h2>Lineage and drug-resistance summary</h2>
      {lineage_table_html}
      <div class="fig-inline" style="margin-top:.9rem">{_figure_card("outbreaker_resistance")}</div>
    </section>

    <section class="card" id="mutations">
      <h2>Drug-resistance mutation details</h2>
      {_blocking_card_html}
      <details open><summary>Local expected gene-drug screen</summary><div>
        <p class="muted">This table is a conservative local screen for obvious mapping errors. It is not a complete WHO catalogue extract and does not validate individual resistance calls.</p>
        <div class="tbl-wrap"><table><thead><tr><th>Drug</th><th>Expected genes</th></tr></thead><tbody>
          <tr><td>Rifampicin</td><td>rpoB</td></tr>
          <tr><td>Isoniazid</td><td>katG, inhA, fabG1</td></tr>
          <tr><td>Pyrazinamide</td><td>pncA</td></tr>
          <tr><td>Ethambutol</td><td>embB</td></tr>
          <tr><td>Fluoroquinolones</td><td>gyrA, gyrB</td></tr>
          <tr><td>Aminoglycosides / injectables</td><td>rrs, eis, tlyA</td></tr>
        </tbody></table></div>
      </div></details>
      {dr_table_html}
    </section>

    <!-- =================================================================== -->
    <!-- CLUSTERS -->
    <!-- =================================================================== -->
    <section class="card" id="clusters">
      <h2>Cluster epidemiology</h2>
      <p class="muted">Genomic summary + growth status for all active clusters. RR/MDR column = rifampicin-resistant / MDR-TB suspected cases.</p>
      <div class="fig-inline">{_figure_card("outbreaker_phylo")}</div>
      {cluster_epi_html}
    </section>

    <section class="card" id="cluster-pri">
      <h2>Cluster prioritisation</h2>
    <div class="section-note">Priority score = case countx2 + cross-region spreadx3 + recency (14/30/60d = 3/2/1) + open statusx3. Scores &gt;10 warrant prioritised MDT review.</div>
      {cluster_pri_html}
    </section>

    <!-- =================================================================== -->
    <!-- METHODS -->
    <!-- =================================================================== -->
    <section class="card" id="methods">
      <h2>Method comparison &amp; secondary engines</h2>
      <h3>Cross-method clustering comparison</h3>
      {comp_html}
      <h3>Sequence clustering snapshot</h3>
      {seq_cluster_html}
    </section>

    <!-- DATA PROVENANCE -->
    <section class="card" id="provenance">
      <h2>Data provenance and reproducibility</h2>
      <div class="section-note">Fields marked <span class="badge badge-red">Missing &#8212; required</span> are mandatory for external circulation. Fields marked <span class="badge badge-grey">Not recorded</span> are optional but strongly recommended for audit and reproducibility.</div>
      {prov_html}
      {'<div class="callout callout-alert" style="margin-top:.6rem"><strong>' + str(len(missing_repro)) + ' required field(s) missing.</strong> External circulation is blocked until all mandatory reproducibility fields are populated. Populate via <code>sequencing_runs</code> or <code>analysis_provenance</code> database tables.</div>' if missing_repro else '<div class="callout" style="margin-top:.6rem">All mandatory reproducibility fields are present. Confirm catalogue and tool versions with the bioinformatics lead before circulation.</div>'}
    </section>

    <!-- EPIDEMIOLOGICAL DATA COMPLETENESS -->
    <section class="card" id="epi-completeness">
      <h2>Epidemiological data completeness</h2>
      <div class="section-note">Completeness of fields capturable from the genomic pipeline. Additional clinical and field epi data must be completed in the case management system.</div>
      {epi_complete_html}
      <h3>Mandatory epi-completeness &#8212; by cluster</h3>
      <div class="section-note">RAG status: <span class="badge badge-red">Red &#8212; incomplete</span> = minimum fields not confirmed in case management system. No cluster may be signed off at MDT until all Red fields are resolved.</div>
      {_cluster_epi_complete_html}
      <h3>Minimum epi fields required before MDT sign-off</h3>
      <div class="tbl-wrap"><table><thead><tr><th>Epi domain</th><th>Minimum fields required</th><th>Source</th><th>Status</th></tr></thead>
      <tbody>
        <tr style="background:var(--alert-bg)"><td>Demographics</td><td>Age band, sex, country of birth, time in UK (&ge;1 year / &lt;1 year)</td><td>Case management system</td><td><span class="badge badge-red">Not confirmed &#8212; complete before MDT</span></td></tr>
        <tr style="background:var(--alert-bg)"><td>Clinical infectiousness</td><td>Pulmonary / extrapulmonary, smear status, cavitation on CXR</td><td>Microbiology / Radiology</td><td><span class="badge badge-red">Not confirmed &#8212; complete before MDT</span></td></tr>
        <tr style="background:var(--alert-bg)"><td>Exposure setting</td><td>At least one confirmed shared setting (household, workplace, congregation)</td><td>TB nurses / HPT</td><td><span class="badge badge-red">Not confirmed &#8212; complete before MDT</span></td></tr>
        <tr style="background:var(--alert-bg)"><td>Contact tracing</td><td>Named contacts listed; tracing initiated or documented as not applicable</td><td>Contact tracing team</td><td><span class="badge badge-red">Not confirmed &#8212; complete before MDT</span></td></tr>
        <tr style="background:var(--alert-bg)"><td>Vulnerability factors</td><td>Homelessness, substance use, immunosuppression screened (yes / no / unknown)</td><td>TB nurses / Social care</td><td><span class="badge badge-red">Not confirmed &#8212; complete before MDT</span></td></tr>
        <tr style="background:var(--alert-bg)"><td>Case timeline</td><td>Symptom onset (est.), diagnosis date, treatment start, sequencing date</td><td>Clinical / Lab records</td><td><span class="badge badge-red">Not confirmed &#8212; complete before MDT</span></td></tr>
      </tbody></table></div>
    </section>

    {appendix_sections_html}
    {figures_section_html}
    {concepts_section_html}

    {raw_artifacts_html}

    <!-- FOOTER -->
    <section class="card" style="font-size:.82rem;color:var(--muted)">
      <p>Generated from TB Genomics backend artifacts &mdash; {_safe_html(generated_at)}. For operational use, interpret genomic findings with clinical history, contact tracing, epidemiology, QC status, and local governance review. This is a decision-support tool only.</p>
    </section>
  </main>
</div>

<script>
/* -- Mutation table: add Validation status column -- */
(function(){{
  var mutTable = document.querySelector('#mutations table');
  if(!mutTable) return;
  var headerRow = mutTable.querySelector('thead tr');
  if(headerRow){{
    var th = document.createElement('th');
    th.textContent = 'Validation status';
    headerRow.appendChild(th);
  }}
  mutTable.querySelectorAll('tbody tr').forEach(function(row){{
    var statusCell = row.querySelector('td:nth-child(5)');
    var td = document.createElement('td');
    if(statusCell && statusCell.textContent.indexOf('Unusual') !== -1){{
      td.innerHTML = "<span class='badge badge-red'>Suppressed</span>";
    }} else {{
      td.innerHTML = "<span class='badge badge-amber'>Not validated</span>";
    }}
    row.appendChild(td);
  }});
}})();

/* -- Active nav highlight -- */
(function(){{
  const links = document.querySelectorAll('nav.sidebar a');
  const sections = Array.from(links).map(a => document.querySelector(a.getAttribute('href'))).filter(Boolean);
  const obs = new IntersectionObserver(entries => {{
    entries.forEach(entry => {{
      if(entry.isIntersecting){{
        links.forEach(l => l.classList.remove('active'));
        const active = document.querySelector('nav.sidebar a[href="#'+entry.target.id+'"]');
        if(active) active.classList.add('active');
      }}
    }});
  }}, {{rootMargin: '-20% 0px -70% 0px'}});
  sections.forEach(s => obs.observe(s));
}})();

/* -- Lightbox -- */
const _figMeta = {{
  outbreaker_trace: {{title:'MCMC Log-Likelihood Trace', dl:'outbreaker_trace.png'}},
  outbreaker_hist:  {{title:'MCMC Log-Likelihood Distribution', dl:'outbreaker_hist.png'}},
  outbreaker_tree:  {{title:'Posterior Transmission Tree', dl:'outbreaker_tree.png'}},
  outbreaker_phylo: {{title:'Hierarchical Clustering Dendrogram', dl:'outbreaker_phylo.png'}},
  outbreaker_resistance: {{title:'Drug Resistance Profile Heatmap', dl:'outbreaker_resistance.png'}},
}};
function openLightbox(stem) {{
  const lb = document.getElementById('lightbox');
  const img = document.getElementById('lb-img');
  const cap = document.getElementById('lb-caption');
  const dlk = document.getElementById('lb-dl');
  const srcImg = document.querySelector('[data-stem="'+stem+'"] .fig-img');
  if(!srcImg) return;
  const meta = _figMeta[stem] || {{title: stem, dl: stem+'.png'}};
  img.src = srcImg.src;
  img.alt = meta.title;
  cap.textContent = meta.title;
  dlk.href = srcImg.src;
  dlk.download = meta.dl;
  lb.showModal();
}}
/* Close on backdrop click */
document.addEventListener('DOMContentLoaded', function(){{
  const lb = document.getElementById('lightbox');
  if(lb) lb.addEventListener('click', function(e){{
    if(e.target === lb) lb.close();
  }});
}});
</script>
</body>
</html>"""

    output_path = _export_path(report_filename)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return html



