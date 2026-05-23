import html as html_lib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.data_safety import get_data_safety_status
from backend.routers.dependencies import get_db
from backend.routers.case_overview import data_readiness, surveillance_kpis
from backend.runtime_paths import export_path
from backend.synthesis.transmission_synthesis import (
    SynthesisConfig,
    build_cluster_risk_summary,
    build_transmission_synthesis,
)


router = APIRouter(prefix="/reports", tags=["reports"])

def _export_path(*parts: str) -> str:
    return export_path(*parts)


def _h(value: Any) -> str:
    return html_lib.escape("" if value is None else str(value), quote=True)


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.1f}%"
    except Exception:
        return _h(value)


def _fmt_date(value: Any) -> str:
    return "n/a" if value in (None, "") else str(value)


def _first_present(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return default


def _as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _read_json_export(filename: str) -> dict[str, Any]:
    path = _export_path(filename)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def _recent_investigation_actions(db: Session, limit: int = 20) -> list[dict[str, Any]]:
    try:
        rows = db.execute(
            text(
                """
                SELECT cluster_id::text, risk_score, risk_band, assigned_to, status,
                       decision, decision_by, decision_at, updated_at, actions
                FROM cluster_investigations
                ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    for row in rows:
        actions = row["actions"] or []
        items.append(
            {
                "cluster_id": str(row["cluster_id"]),
                "cluster_short": str(row["cluster_id"])[:8],
                "risk_score": float(row["risk_score"] or 0),
                "risk_band": str(row["risk_band"] or "unknown"),
                "assigned_to": row["assigned_to"],
                "status": row["status"] or "open",
                "decision": row["decision"],
                "decision_by": row["decision_by"],
                "decision_at": row["decision_at"].isoformat() if row["decision_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                "action_count": len(actions) if isinstance(actions, list) else 0,
                "last_actions": actions[-3:] if isinstance(actions, list) else [],
            }
        )
    return items


def _recent_alerts(db: Session, limit: int = 20) -> list[dict[str, Any]]:
    try:
        rows = db.execute(
            text(
                """
                SELECT alert_id::text AS alert_id, alert_type, severity, status,
                       sample_id::text AS sample_id, cluster_id::text AS cluster_id,
                       title, assigned_to, created_at
                FROM alerts
                ORDER BY
                  CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                  created_at DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
    except Exception:
        return []

    return [
        {
            "alert_id": str(row["alert_id"]),
            "alert_short": str(row["alert_id"])[:8],
            "type": row["alert_type"],
            "severity": row["severity"],
            "status": row["status"],
            "sample": str(row["sample_id"])[:8] if row["sample_id"] else "",
            "cluster": str(row["cluster_id"])[:8] if row["cluster_id"] else "",
            "title": row["title"],
            "assigned_to": row["assigned_to"] or "Unassigned",
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]


def _recent_actions(db: Session, limit: int = 20) -> list[dict[str, Any]]:
    try:
        rows = db.execute(
            text(
                """
                SELECT action_id::text AS action_id, alert_id::text AS alert_id,
                       cluster_id::text AS cluster_id, sample_id::text AS sample_id,
                       action_type, status, owner, note, due_at, completed_at
                FROM actions
                ORDER BY COALESCE(due_at, created_at) ASC NULLS LAST
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
    except Exception:
        return []

    return [
        {
            "action_id": str(row["action_id"]),
            "action_short": str(row["action_id"])[:8],
            "alert": str(row["alert_id"])[:8] if row["alert_id"] else "",
            "cluster": str(row["cluster_id"])[:8] if row["cluster_id"] else "",
            "sample": str(row["sample_id"])[:8] if row["sample_id"] else "",
            "action_type": row["action_type"],
            "status": row["status"],
            "owner": row["owner"] or "Unassigned",
            "note": row["note"] or "",
            "due_at": row["due_at"].isoformat() if row["due_at"] else None,
            "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
        }
        for row in rows
    ]


def _resistance_call_summary(db: Session, limit: int = 20) -> dict[str, Any]:
    try:
        summary = db.execute(
            text(
                """
                SELECT
                    COUNT(*) AS call_count,
                    COUNT(DISTINCT sample_id) AS sample_count,
                    COUNT(*) FILTER (
                      WHERE UPPER(COALESCE(prediction, '')) IN ('R', 'RESISTANT')
                         OR LOWER(COALESCE(prediction, '')) LIKE '%resistant%'
                    ) AS resistant_call_count,
                    COUNT(DISTINCT database_version) FILTER (WHERE database_version IS NOT NULL) AS database_version_count
                FROM resistance_calls
                """
            )
        ).mappings().first()
        rows = db.execute(
            text(
                """
                SELECT sample_id::text AS sample_id, drug, NULLIF(gene, '') AS gene,
                       NULLIF(mutation, '') AS mutation, prediction, confidence,
                       depth, alt_fraction, lineage, source_tool, tool_version, database_version
                FROM resistance_calls
                ORDER BY created_at DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
    except Exception:
        return {"call_count": 0, "sample_count": 0, "resistant_call_count": 0, "database_version_count": 0, "recent_calls": []}

    return {
        "call_count": int(summary["call_count"] or 0),
        "sample_count": int(summary["sample_count"] or 0),
        "resistant_call_count": int(summary["resistant_call_count"] or 0),
        "database_version_count": int(summary["database_version_count"] or 0),
        "recent_calls": [
            {
                "sample": str(row["sample_id"])[:8],
                "drug": row["drug"],
                "gene": row["gene"] or "",
                "mutation": row["mutation"] or "",
                "prediction": row["prediction"] or "",
                "confidence": row["confidence"],
                "depth": row["depth"],
                "alt_fraction": row["alt_fraction"],
                "lineage": row["lineage"] or "",
                "source": row["source_tool"] or "",
                "tool_version": row["tool_version"] or "",
                "database_version": row["database_version"] or "",
            }
            for row in rows
        ],
    }


def _analysis_provenance(db: Session, limit: int = 8) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        rows = db.execute(
            text(
                """
                SELECT sample_id::text, pipeline_name, pipeline_version,
                       reference_genome, software_versions, parameters, generated_at
                FROM analysis_provenance
                ORDER BY generated_at DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
    except Exception:
        rows = []

    for row in rows:
        parameters = _as_mapping(row["parameters"])
        software_versions = _as_mapping(row["software_versions"])
        generated_at = row["generated_at"]
        sample_id = str(row["sample_id"]) if row["sample_id"] else "run"
        items.append(
            {
                "sample_id": sample_id,
                "sample_short": sample_id[:8],
                "pipeline_name": row["pipeline_name"],
                "pipeline_version": row["pipeline_version"],
                "reference_genome": row["reference_genome"],
                "resistance_catalogue": _first_present(
                    parameters.get("resistance_catalogue"),
                    parameters.get("catalogue_version"),
                    software_versions.get("resistance_catalogue"),
                    software_versions.get("catalogue_version"),
                    default="n/a",
                ),
                "analysis_date": generated_at.isoformat() if generated_at else None,
                "source": "database",
            }
        )
    return (items + _artifact_provenance_rows())[:limit]


def _artifact_provenance_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    outbreaker = _read_json_export("outbreaker_summary.json")
    if outbreaker:
        iteration = _as_mapping(outbreaker.get("mcmc_iteration_config"))
        rows.append(
            {
                "sample_id": "run",
                "sample_short": "run",
                "pipeline_name": outbreaker.get("analysis_engine", "outbreaker2"),
                "pipeline_version": f"{iteration.get('n_iter_total', 'n/a')} iterations",
                "reference_genome": "transmission posterior",
                "resistance_catalogue": "n/a",
                "analysis_date": outbreaker.get("generated_at"),
                "source": "artifact",
            }
        )

    lineage = _read_json_export("lineage_dr_validation.json")
    if lineage:
        resistance_block = _as_mapping(lineage.get("resistance_validation"))
        selected_fastas = _as_mapping(lineage.get("inputs")).get("selected_fasta_files") or []
        rows.append(
            {
                "sample_id": "run",
                "sample_short": "run",
                "pipeline_name": "lineage/dr validation",
                "pipeline_version": lineage.get("scaffold_version", lineage.get("status", "n/a")),
                "reference_genome": (
                    os.path.basename(selected_fastas[0]) if selected_fastas else "sequence exports"
                ),
                "resistance_catalogue": _first_present(
                    resistance_block.get("catalogue_version"),
                    _read_json_export("resistance_validation.json").get("catalogue_version"),
                    default="n/a",
                ),
                "analysis_date": lineage.get("generated_at"),
                "source": "artifact",
            }
        )

    fasta_analysis = _read_json_export("fasta_analysis_summary.json")
    if fasta_analysis:
        tools = _as_mapping(fasta_analysis.get("tools"))
        available_tools = [
            key
            for key, value in tools.items()
            if key != "_wsl" and isinstance(value, dict) and value.get("status") == "available"
        ]
        rows.append(
            {
                "sample_id": "run",
                "sample_short": "run",
                "pipeline_name": "advanced FASTA analysis",
                "pipeline_version": fasta_analysis.get("status", "n/a"),
                "reference_genome": os.path.basename(str(fasta_analysis.get("input_fasta") or "sequence exports")),
                "resistance_catalogue": "n/a",
                "analysis_date": fasta_analysis.get("generated_at"),
                "source": "artifact: " + (", ".join(available_tools) if available_tools else "no optional tools available"),
            }
        )

    resistance = _read_json_export("resistance_validation.json")
    if resistance:
        rows.append(
            {
                "sample_id": "run",
                "sample_short": "run",
                "pipeline_name": "resistance validation",
                "pipeline_version": resistance.get("pipeline_validation_status", resistance.get("status", "n/a")),
                "reference_genome": resistance.get("validation_scope", "local mapping screen"),
                "resistance_catalogue": resistance.get("catalogue_version", "n/a"),
                "analysis_date": resistance.get("generated_at"),
                "source": "artifact",
            }
        )

    synthesis = _read_json_export("synthesis_output.json")
    if synthesis:
        rows.append(
            {
                "sample_id": "run",
                "sample_short": "run",
                "pipeline_name": "transmission synthesis",
                "pipeline_version": f"format {synthesis.get('format_version', 'n/a')}",
                "reference_genome": "cluster and transmission exports",
                "resistance_catalogue": "n/a",
                "analysis_date": synthesis.get("generated_at"),
                "source": "artifact",
            }
        )

    return rows


def _lineage_dr_summary() -> dict[str, Any]:
    payload = _read_json_export("lineage_dr_validation.json")
    if not payload:
        return {"status": "not_available", "message": "Lineage/DR validation artifact not found."}

    resistance_payload = _read_json_export("resistance_validation.json")
    concordance = _as_mapping(payload.get("dr_concordance"))
    resistance_block = _as_mapping(payload.get("resistance_validation"))
    resistance_summary = _as_mapping(
        _first_present(
            resistance_block.get("summary"),
            resistance_payload.get("summary"),
            default={},
        )
    )
    tbprofiler_run = _as_mapping(payload.get("tbprofiler_run"))
    mykrobe_run = _as_mapping(payload.get("mykrobe_run"))
    tbprofiler_import = _as_mapping(payload.get("tbprofiler_db_import"))
    mykrobe_import = _as_mapping(payload.get("mykrobe_db_import"))
    db_imported_rows = (tbprofiler_import.get("imported_rows") or 0) + (
        mykrobe_import.get("imported_rows") or 0
    )
    resistance_call_rows = tbprofiler_import.get("resistance_call_rows") or 0

    return {
        "status": payload.get("status", "available"),
        "confidence": payload.get("confidence", "n/a"),
        "interpretation_blocking": payload.get("interpretation_blocking"),
        "validated_calls": resistance_summary.get("validated_calls"),
        "total_mutation_calls": resistance_summary.get("total_mutation_calls"),
        "unusual_gene_drug_mapping_calls": resistance_summary.get("unusual_gene_drug_mapping_calls"),
        "suppressed_calls": resistance_summary.get("suppressed_calls"),
        "discordant_samples": concordance.get("discordant_sample_count"),
        "compared_samples": concordance.get("samples_compared"),
        "comparable_drug_calls": concordance.get("comparable_drug_calls"),
        "one_tool_no_call_sample_count": concordance.get("one_tool_no_call_sample_count"),
        "concordance_interpretable": concordance.get("concordance_interpretable"),
        "tbprofiler_run": (
            f"{tbprofiler_run.get('status', 'n/a')} "
            f"({tbprofiler_run.get('successful_samples', 0)}/"
            f"{tbprofiler_run.get('attempted_samples', 0)})"
        ),
        "mykrobe_run": (
            f"{mykrobe_run.get('status', 'n/a')} "
            f"({mykrobe_run.get('successful_samples', 0)}/"
            f"{mykrobe_run.get('attempted_samples', 0)})"
        ),
        "tbprofiler_runner": tbprofiler_run.get("runner", "n/a"),
        "mykrobe_runner": mykrobe_run.get("runner", "n/a"),
        "db_imported_rows": db_imported_rows,
        "resistance_call_rows": resistance_call_rows,
        "catalogue_version": resistance_payload.get("catalogue_version", "n/a"),
        "pipeline_validation_status": resistance_payload.get(
            "pipeline_validation_status",
            resistance_block.get("pipeline_validation_status", "n/a"),
        ),
        "warnings": list(payload.get("warnings") or []),
        "limitation_codes": list(payload.get("limitation_codes") or []),
        "artifact_status": payload.get("status", "available"),
    }


def _suggested_investigation_actions(
    cluster_dossiers: list[dict[str, Any]], immediate_actions: list[str]
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for action in immediate_actions:
        items.append(
            {
                "cluster": "All",
                "band": "report",
                "priority": "",
                "action": action,
                "source": "executive summary",
            }
        )
    for dossier in cluster_dossiers:
        summary = dossier.get("summary") or {}
        for action in dossier.get("recommended_actions") or []:
            items.append(
                {
                    "cluster": dossier.get("cluster_short")
                    or str(dossier.get("cluster_id") or "")[:8],
                    "band": summary.get("priority_band", ""),
                    "priority": summary.get("priority_score", ""),
                    "action": action,
                    "source": "cluster synthesis",
                }
            )
    return items[:20]


def _synthesis_config(
    *,
    min_posterior: float,
    low_snp_threshold: int,
    temporal_window_days: int,
) -> SynthesisConfig:
    return SynthesisConfig(
        min_posterior=min_posterior,
        low_snp_threshold=low_snp_threshold,
        temporal_window_days=temporal_window_days,
    )


def build_actionable_surveillance_report(
    db: Session,
    *,
    weeks: int = 12,
    top_clusters: int = 5,
    min_posterior: float = 0.0,
    low_snp_threshold: int = 12,
    temporal_window_days: int = 45,
) -> dict[str, Any]:
    cfg = _synthesis_config(
        min_posterior=min_posterior,
        low_snp_threshold=low_snp_threshold,
        temporal_window_days=temporal_window_days,
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    safety = get_data_safety_status(db)
    readiness = data_readiness(db=db)
    kpis = surveillance_kpis(weeks=weeks, db=db)
    risk_summary = build_cluster_risk_summary(db=db, config=cfg)
    alerts = _recent_alerts(db)
    actions = _recent_actions(db)
    resistance_calls = _resistance_call_summary(db)

    ranked_clusters = list(risk_summary.get("clusters", []))[: max(1, top_clusters)]
    cluster_dossiers = []
    for cluster in ranked_clusters:
        cluster_id = cluster.get("cluster_id")
        summary_fallback = {
            "member_count": cluster.get("member_count", 0),
            "pair_count": cluster.get("pair_count", 0),
            "priority_score": cluster.get("priority_score", 0),
            "priority_band": cluster.get("priority_band", "low"),
            "regions": [],
            "resistance_case_count": 0,
            "first_specimen": None,
            "last_specimen": None,
        }
        dossier: dict[str, Any] = {
            "cluster_id": cluster_id,
            "cluster_short": cluster.get("cluster_short") or str(cluster_id or "")[:8],
            "summary": summary_fallback,
            "flags": list(cluster.get("flags") or []),
            "recommended_actions": list(cluster.get("top_recommended_actions") or []),
            "confidence_counts": {},
            "top_pairs": [],
        }

        if not cluster_id or not _is_uuid(cluster_id):
            dossier["note"] = "Detailed synthesis is unavailable because this cluster identifier is not a UUID-backed investigation cluster."
            cluster_dossiers.append(dossier)
            continue

        try:
            synthesis = build_transmission_synthesis(db=db, cluster_id=cluster_id, config=cfg)
            details = (synthesis.get("clusters") or [{}])[0]
            summary = details.get("summary") or summary_fallback
            top_pairs = list(details.get("pairwise_transmission_evidence") or [])[:5]
            dossier.update(
                {
                    "summary": summary,
                    "flags": details.get("flags", dossier["flags"]),
                    "recommended_actions": details.get("recommended_investigation_actions", dossier["recommended_actions"]),
                    "confidence_counts": details.get("confidence_counts", {}),
                    "top_pairs": top_pairs,
                }
            )
        except Exception:
            dossier["note"] = "Detailed synthesis could not be loaded for this cluster at report time; summary-level triage data is shown instead."

        cluster_dossiers.append(dossier)

    urgent_clusters = [
        c
        for c in ranked_clusters
        if str(c.get("priority_band", "")).lower() in {"high", "critical"}
        or int(c.get("priority_score") or 0) >= 70
    ]
    report_status = "ready"
    if not safety.get("operational_safe"):
        report_status = "blocked_non_operational"
    elif readiness.get("status") != "ready":
        report_status = "needs_data_review"

    immediate_actions = []
    if not safety.get("operational_safe"):
        immediate_actions.append("Do not use for operational public-health action until demo/synthetic signals are removed.")
    if readiness.get("status") != "ready":
        immediate_actions.append("Review missing data fields before interpreting priority scores.")
    if urgent_clusters:
        immediate_actions.append("Assign reviewers to high-priority clusters and record actions in the investigation workflow.")
    if not immediate_actions:
        immediate_actions.append("Continue routine surveillance review and monitor new clusters.")

    return {
        "report_type": "actionable_surveillance_report",
        "generated_at": generated_at,
        "parameters": {
            "weeks": weeks,
            "top_clusters": top_clusters,
            "min_posterior": min_posterior,
            "low_snp_threshold": low_snp_threshold,
            "temporal_window_days": temporal_window_days,
        },
        "status": report_status,
        "executive_summary": {
            "total_cases": safety.get("total_cases", 0),
            "operational_mode": safety.get("mode"),
            "operational_safe": bool(safety.get("operational_safe")),
            "readiness_status": readiness.get("status"),
            "cluster_count": risk_summary.get("summary", {}).get("cluster_count", 0),
            "high_priority_pairs": risk_summary.get("summary", {}).get("high_priority_pairs", 0),
            "contradictory_pairs": risk_summary.get("summary", {}).get("contradictory_pairs", 0),
            "urgent_cluster_count": len(urgent_clusters),
            "open_alert_count": sum(1 for alert in alerts if alert.get("status") in {"open", "acknowledged"}),
            "critical_alert_count": sum(1 for alert in alerts if alert.get("severity") == "critical"),
            "resistant_call_count": resistance_calls.get("resistant_call_count", 0),
            "immediate_actions": immediate_actions,
        },
        "data_safety": safety,
        "data_readiness": readiness,
        "surveillance_kpis": kpis,
        "priority_clusters": ranked_clusters,
        "cluster_dossiers": cluster_dossiers,
        "investigation_activity": _recent_investigation_actions(db),
        "alerts": alerts,
        "actions": actions,
        "resistance_calls": resistance_calls,
        "suggested_investigation_actions": _suggested_investigation_actions(cluster_dossiers, immediate_actions),
        "lineage_dr_summary": _lineage_dr_summary(),
        "analysis_provenance": _analysis_provenance(db),
        "validation_status": risk_summary.get("validation_status", "heuristic_non_validated"),
        "warning": risk_summary.get(
            "warning",
            "This report is decision support only. Scores and transmission interpretations are heuristic and non-validated.",
        ),
    }


def _metric_card(label: str, value: Any, note: str = "") -> str:
    return (
        '<div class="metric">'
        f"<span>{_h(label)}</span>"
        f"<strong>{_h(value)}</strong>"
        f"<em>{_h(note)}</em>"
        "</div>"
    )


def _simple_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], empty: str) -> str:
    if not rows:
        return f'<p class="muted">{_h(empty)}</p>'
    header = "".join(f"<th>{_h(label)}</th>" for label, _ in columns)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{_h(row.get(key, ''))}</td>" for _, key in columns) + "</tr>")
    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_actionable_surveillance_report_html(report: dict[str, Any]) -> str:
    summary = report.get("executive_summary") or {}
    kpis = report.get("surveillance_kpis") or {}
    readiness = report.get("data_readiness") or {}
    lineage = report.get("lineage_dr_summary") or {}

    action_items = "".join(f"<li>{_h(action)}</li>" for action in summary.get("immediate_actions", []))
    readiness_rows = [
        {
            "check": check.get("label"),
            "complete": check.get("complete"),
            "missing": check.get("missing"),
            "percent": _fmt_pct(check.get("percent")),
        }
        for check in readiness.get("checks", [])
    ]
    priority_rows = [
        {
            "cluster": item.get("cluster_short"),
            "cases": item.get("member_count"),
            "score": item.get("priority_score"),
            "band": item.get("priority_band"),
            "flags": ", ".join(item.get("flags") or []),
            "actions": "; ".join(item.get("top_recommended_actions") or []),
        }
        for item in report.get("priority_clusters", [])
    ]
    investigation_rows = [
        {
            "cluster": item.get("cluster_short"),
            "band": item.get("risk_band"),
            "assigned": item.get("assigned_to") or "Unassigned",
            "status": item.get("status"),
            "decision": item.get("decision") or "",
            "actions": item.get("action_count"),
        }
        for item in report.get("investigation_activity", [])
    ]
    suggested_action_rows = [
        {
            "cluster": item.get("cluster"),
            "band": item.get("band"),
            "priority": item.get("priority"),
            "action": item.get("action"),
            "source": item.get("source"),
        }
        for item in report.get("suggested_investigation_actions", [])
    ]
    provenance_rows = [
        {
            "sample": item.get("sample_short"),
            "pipeline": item.get("pipeline_name"),
            "version": item.get("pipeline_version"),
            "reference": item.get("reference_genome"),
            "catalogue": item.get("resistance_catalogue"),
            "date": _fmt_date(item.get("analysis_date")),
            "source": item.get("source", ""),
        }
        for item in report.get("analysis_provenance", [])
    ]
    lineage_warning_parts = list(lineage.get("warnings") or [])
    if lineage.get("limitation_codes"):
        lineage_warning_parts.append("Limitations: " + ", ".join(lineage.get("limitation_codes") or []))
    lineage_warning = " ".join(lineage_warning_parts)
    alert_rows = [
        {
            "severity": item.get("severity"),
            "status": item.get("status"),
            "type": item.get("type"),
            "target": item.get("sample") or item.get("cluster"),
            "title": item.get("title"),
            "assigned": item.get("assigned_to"),
        }
        for item in report.get("alerts", [])
    ]
    action_rows = [
        {
            "action": item.get("action_type"),
            "status": item.get("status"),
            "owner": item.get("owner"),
            "target": item.get("sample") or item.get("cluster") or item.get("alert"),
            "due": _fmt_date(item.get("due_at")),
            "note": item.get("note"),
        }
        for item in report.get("actions", [])
    ]
    resistance_block = report.get("resistance_calls") or {}
    resistance_rows = [
        {
            "sample": item.get("sample"),
            "drug": item.get("drug"),
            "gene": item.get("gene"),
            "mutation": item.get("mutation"),
            "prediction": item.get("prediction"),
            "depth": item.get("depth"),
            "alt_fraction": item.get("alt_fraction"),
            "source": item.get("source"),
            "database": item.get("database_version"),
        }
        for item in resistance_block.get("recent_calls", [])
    ]

    dossier_sections = []
    for dossier in report.get("cluster_dossiers", []):
        ds = dossier.get("summary") or {}
        pair_rows = [
            {
                "pair": f"{str(pair.get('source', ''))[:8]} -> {str(pair.get('target', ''))[:8]}",
                "confidence": pair.get("confidence"),
                "priority": pair.get("priority_score"),
                "posterior": pair.get("posterior_probability"),
                "snp": pair.get("snp_distance"),
                "interpretation": pair.get("interpretation"),
            }
            for pair in dossier.get("top_pairs", [])
        ]
        dossier_sections.append(
            f"""
            <section>
              <h2>Cluster { _h(dossier.get('cluster_short')) }</h2>
              <div class="grid">
                {_metric_card("Cases", ds.get("member_count", 0))}
                {_metric_card("Priority", ds.get("priority_score", 0), ds.get("priority_band", ""))}
                {_metric_card("Regions", ", ".join(ds.get("regions") or []) or "n/a")}
                {_metric_card("Resistance signals", ds.get("resistance_case_count", 0))}
              </div>
              <p><strong>Period:</strong> {_h(_fmt_date(ds.get("first_specimen")))} to {_h(_fmt_date(ds.get("last_specimen")))}</p>
              <p><strong>Flags:</strong> {_h(", ".join(dossier.get("flags") or []) or "None")}</p>
              <p><strong>Recommended actions:</strong> {_h("; ".join(dossier.get("recommended_actions") or []) or "Continue review")}</p>
              {f'<p class="muted"><strong>Note:</strong> {_h(dossier.get("note"))}</p>' if dossier.get("note") else ''}
              {_simple_table(pair_rows, [
                  ("Pair", "pair"),
                  ("Confidence", "confidence"),
                  ("Priority", "priority"),
                  ("Posterior", "posterior"),
                  ("SNP", "snp"),
                  ("Interpretation", "interpretation"),
              ], "No pairwise evidence available for this cluster.")}
            </section>
            """
        )

    status_class = "blocked" if report.get("status") == "blocked_non_operational" else "review" if report.get("status") == "needs_data_review" else "ready"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Actionable TB Genomic Surveillance Report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f6f7f9;color:#172033;}}
header{{background:#19324d;color:white;padding:28px 36px;}}
header h1{{margin:0 0 6px;font-size:26px;}}
header p{{margin:0;color:#d7e2ed;}}
main{{padding:24px 36px 40px;}}
section{{background:white;border:1px solid #dfe4ea;border-radius:8px;padding:18px 20px;margin-bottom:16px;}}
h2{{font-size:18px;margin:0 0 12px;color:#19324d;}}
h3{{font-size:15px;margin:16px 0 8px;color:#2f4154;}}
.banner{{border-radius:8px;padding:12px 14px;margin:0 0 16px;font-weight:700;}}
.ready{{background:#e7f6ed;color:#166534;border:1px solid #86efac;}}
.review{{background:#fff7ed;color:#9a3412;border:1px solid #fdba74;}}
.blocked{{background:#fee2e2;color:#991b1b;border:1px solid #fca5a5;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:10px 0 12px;}}
.metric{{border:1px solid #e5e9ef;border-radius:8px;padding:10px 12px;background:#fbfcfd;}}
.metric span{{display:block;font-size:12px;color:#617080;}}
.metric strong{{display:block;font-size:22px;margin-top:3px;}}
.metric em{{display:block;font-size:12px;color:#617080;font-style:normal;}}
table{{width:100%;border-collapse:collapse;font-size:13px;}}
th{{text-align:left;background:#eef2f6;color:#29394a;padding:7px;border-bottom:1px solid #d8dee6;}}
td{{padding:7px;border-bottom:1px solid #edf0f3;vertical-align:top;}}
ul{{margin:8px 0 0 20px;padding:0;}}
.muted{{color:#64748b;}}
.warning{{font-size:12px;color:#7c2d12;background:#fff7ed;border:1px solid #fdba74;border-radius:8px;padding:10px 12px;}}
@media print{{body{{background:white;}}main{{padding:16px;}}section{{break-inside:avoid;}}}}
</style>
</head>
<body>
<header>
  <h1>Actionable TB Genomic Surveillance Report</h1>
  <p>Generated {_h(report.get("generated_at"))} UTC</p>
</header>
<main>
  <div class="banner {status_class}">Report status: {_h(report.get("status"))}</div>
  <section>
    <h2>Executive Summary</h2>
    <div class="grid">
      {_metric_card("Cases", summary.get("total_cases", 0), summary.get("operational_mode", ""))}
      {_metric_card("Clusters", summary.get("cluster_count", 0))}
      {_metric_card("Urgent clusters", summary.get("urgent_cluster_count", 0))}
      {_metric_card("Open alerts", summary.get("open_alert_count", 0), f"critical={summary.get('critical_alert_count', 0)}")}
      {_metric_card("Resistant calls", summary.get("resistant_call_count", 0))}
      {_metric_card("Contradictory pairs", summary.get("contradictory_pairs", 0))}
    </div>
    <h3>Immediate Actions</h3>
    <ul>{action_items}</ul>
  </section>

  <section>
    <h2>Data Safety And Readiness</h2>
    <p><strong>Operational safe:</strong> {_h(summary.get("operational_safe"))} | <strong>Readiness:</strong> {_h(summary.get("readiness_status"))}</p>
    {_simple_table(readiness_rows, [("Check", "check"), ("Complete", "complete"), ("Missing", "missing"), ("Percent", "percent")], "No readiness checks available.")}
  </section>

  <section>
    <h2>Programme KPIs</h2>
    <div class="grid">
      {_metric_card("Window", f"{kpis.get('window_weeks', 'n/a')} weeks")}
      {_metric_card("Sequenced", kpis.get("sequenced_cases", 0), _fmt_pct(kpis.get("sequenced_pct")))}
      {_metric_card("QC pass", kpis.get("qc_pass_cases", 0), _fmt_pct(kpis.get("qc_pass_pct")))}
      {_metric_card("Median specimen to QC", kpis.get("median_days_specimen_to_qc", "n/a"), "days")}
    </div>
  </section>

  <section>
    <h2>Priority Cluster List</h2>
    {_simple_table(priority_rows, [
        ("Cluster", "cluster"),
        ("Cases", "cases"),
        ("Score", "score"),
        ("Band", "band"),
        ("Flags", "flags"),
        ("Top Actions", "actions"),
    ], "No clusters available.")}
  </section>

  {''.join(dossier_sections)}

  <section>
    <h2>Investigation Activity</h2>
    {_simple_table(investigation_rows, [
        ("Cluster", "cluster"),
        ("Band", "band"),
        ("Assigned", "assigned"),
        ("Status", "status"),
        ("Decision", "decision"),
        ("Actions", "actions"),
    ], "No investigation activity recorded.")}
    <h3>Generated Action Plan</h3>
    {_simple_table(suggested_action_rows, [
        ("Cluster", "cluster"),
        ("Band", "band"),
        ("Priority", "priority"),
        ("Action", "action"),
        ("Source", "source"),
    ], "No generated investigation actions available.")}
  </section>

  <section>
    <h2>Alerts And Action Tracker</h2>
    {_simple_table(alert_rows, [
        ("Severity", "severity"),
        ("Status", "status"),
        ("Type", "type"),
        ("Target", "target"),
        ("Title", "title"),
        ("Assigned", "assigned"),
    ], "No alerts recorded.")}
    <h3>Open Actions</h3>
    {_simple_table(action_rows, [
        ("Action", "action"),
        ("Status", "status"),
        ("Owner", "owner"),
        ("Target", "target"),
        ("Due", "due"),
        ("Note", "note"),
    ], "No actions recorded.")}
  </section>

  <section>
    <h2>Lineage And Drug Resistance</h2>
    <div class="grid">
      {_metric_card("Validation status", lineage.get("status", "not_available"))}
      {_metric_card("Confidence", lineage.get("confidence", "n/a"), f"blocking={lineage.get('interpretation_blocking', 'n/a')}")}
      {_metric_card("Compared samples", lineage.get("compared_samples", "n/a"))}
      {_metric_card("Comparable drug calls", lineage.get("comparable_drug_calls", "n/a"), f"interpretable={lineage.get('concordance_interpretable', 'n/a')}")}
      {_metric_card("Discordant samples", lineage.get("discordant_samples", "n/a"))}
      {_metric_card("Suppressed calls", lineage.get("suppressed_calls", "n/a"))}
      {_metric_card("Validated calls", lineage.get("validated_calls", "n/a"), f"mutations={lineage.get('total_mutation_calls', 'n/a')}")}
      {_metric_card("Unusual mappings", lineage.get("unusual_gene_drug_mapping_calls", "n/a"))}
      {_metric_card("TBProfiler", lineage.get("tbprofiler_run", "n/a"), lineage.get("tbprofiler_runner", ""))}
      {_metric_card("Mykrobe", lineage.get("mykrobe_run", "n/a"), lineage.get("mykrobe_runner", ""))}
      {_metric_card("Imported DR rows", lineage.get("db_imported_rows", "n/a"))}
      {_metric_card("Resistance call rows", lineage.get("resistance_call_rows", "n/a"))}
      {_metric_card("Normalized resistance calls", resistance_block.get("call_count", 0), f"samples={resistance_block.get('sample_count', 0)}")}
      {_metric_card("Catalogue", lineage.get("catalogue_version", "n/a"), lineage.get("pipeline_validation_status", ""))}
    </div>
    {_simple_table(resistance_rows, [
        ("Sample", "sample"),
        ("Drug", "drug"),
        ("Gene", "gene"),
        ("Mutation", "mutation"),
        ("Prediction", "prediction"),
        ("Depth", "depth"),
        ("Alt fraction", "alt_fraction"),
        ("Source", "source"),
        ("Database", "database"),
    ], "No normalized resistance calls recorded.")}
    {f'<p class="warning"><strong>Lineage/DR limitation:</strong> {_h(lineage_warning)}</p>' if lineage_warning else ''}
  </section>

  <section>
    <h2>Provenance</h2>
    {_simple_table(provenance_rows, [
        ("Sample", "sample"),
        ("Pipeline", "pipeline"),
        ("Version", "version"),
        ("Reference", "reference"),
        ("Catalogue", "catalogue"),
        ("Date", "date"),
        ("Source", "source"),
    ], "No analysis provenance records available.")}
  </section>

  <p class="warning"><strong>Decision-support limitation:</strong> {_h(report.get("warning"))}</p>
</main>
</body>
</html>"""


@router.get("/actionable-surveillance")
def actionable_surveillance_report(
    weeks: int = Query(12, ge=1, le=104),
    top_clusters: int = Query(5, ge=1, le=20),
    min_posterior: float = Query(0.0, ge=0.0, le=1.0),
    low_snp_threshold: int = Query(12, ge=1, le=100),
    temporal_window_days: int = Query(45, ge=1, le=365),
    db: Session = Depends(get_db),
):
    return build_actionable_surveillance_report(
        db,
        weeks=weeks,
        top_clusters=top_clusters,
        min_posterior=min_posterior,
        low_snp_threshold=low_snp_threshold,
        temporal_window_days=temporal_window_days,
    )


@router.get("/actionable-surveillance.html", response_class=HTMLResponse)
def actionable_surveillance_report_html(
    weeks: int = Query(12, ge=1, le=104),
    top_clusters: int = Query(5, ge=1, le=20),
    min_posterior: float = Query(0.0, ge=0.0, le=1.0),
    low_snp_threshold: int = Query(12, ge=1, le=100),
    temporal_window_days: int = Query(45, ge=1, le=365),
    db: Session = Depends(get_db),
):
    report = build_actionable_surveillance_report(
        db,
        weeks=weeks,
        top_clusters=top_clusters,
        min_posterior=min_posterior,
        low_snp_threshold=low_snp_threshold,
        temporal_window_days=temporal_window_days,
    )
    html = render_actionable_surveillance_report_html(report)
    os.makedirs(_export_path(), exist_ok=True)
    with open(_export_path("actionable_surveillance_report.html"), "w", encoding="utf-8") as handle:
        handle.write(html)
    return HTMLResponse(content=html)
