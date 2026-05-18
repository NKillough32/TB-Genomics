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
from backend.database import SessionLocal
from backend.routers.case_overview import data_readiness, surveillance_kpis
from backend.synthesis.transmission_synthesis import (
    SynthesisConfig,
    build_cluster_risk_summary,
    build_transmission_synthesis,
)


router = APIRouter(prefix="/reports", tags=["reports"])

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _export_path(*parts: str) -> str:
    return os.path.join(PROJECT_ROOT, "exports", *parts)


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


def _analysis_provenance(db: Session, limit: int = 8) -> list[dict[str, Any]]:
    try:
        rows = db.execute(
            text(
                """
                SELECT sample_id::text, pipeline_name, pipeline_version,
                       reference_genome, resistance_catalogue, analysis_date
                FROM analysis_provenance
                ORDER BY analysis_date DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
    except Exception:
        return []

    return [
        {
            "sample_id": str(row["sample_id"]),
            "sample_short": str(row["sample_id"])[:8],
            "pipeline_name": row["pipeline_name"],
            "pipeline_version": row["pipeline_version"],
            "reference_genome": row["reference_genome"],
            "resistance_catalogue": row["resistance_catalogue"],
            "analysis_date": row["analysis_date"].isoformat() if row["analysis_date"] else None,
        }
        for row in rows
    ]


def _lineage_dr_summary() -> dict[str, Any]:
    payload = _read_json_export("lineage_dr_validation.json")
    if not payload:
        return {"status": "not_available", "message": "Lineage/DR validation artifact not found."}

    concordance = payload.get("dr_concordance") or {}
    summary = payload.get("summary") or {}
    return {
        "status": payload.get("status", "available"),
        "validated_records": summary.get("validated_records"),
        "suppressed_calls": summary.get("suppressed_calls"),
        "discordant_samples": concordance.get("discordant_samples"),
        "compared_samples": concordance.get("compared_samples"),
        "artifact_status": payload.get("status", "available"),
    }


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

    ranked_clusters = list(risk_summary.get("clusters", []))[: max(1, top_clusters)]
    cluster_dossiers = []
    for cluster in ranked_clusters:
        cluster_id = cluster.get("cluster_id")
        if not cluster_id or not _is_uuid(cluster_id):
            continue
        synthesis = build_transmission_synthesis(db=db, cluster_id=cluster_id, config=cfg)
        details = (synthesis.get("clusters") or [{}])[0]
        summary = details.get("summary") or {}
        top_pairs = list(details.get("pairwise_transmission_evidence") or [])[:5]
        cluster_dossiers.append(
            {
                "cluster_id": cluster_id,
                "cluster_short": cluster.get("cluster_short") or str(cluster_id)[:8],
                "summary": summary,
                "flags": details.get("flags", []),
                "recommended_actions": details.get("recommended_investigation_actions", []),
                "confidence_counts": details.get("confidence_counts", {}),
                "top_pairs": top_pairs,
            }
        )

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
            "immediate_actions": immediate_actions,
        },
        "data_safety": safety,
        "data_readiness": readiness,
        "surveillance_kpis": kpis,
        "priority_clusters": ranked_clusters,
        "cluster_dossiers": cluster_dossiers,
        "investigation_activity": _recent_investigation_actions(db),
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
    provenance_rows = [
        {
            "sample": item.get("sample_short"),
            "pipeline": item.get("pipeline_name"),
            "version": item.get("pipeline_version"),
            "reference": item.get("reference_genome"),
            "catalogue": item.get("resistance_catalogue"),
            "date": _fmt_date(item.get("analysis_date")),
        }
        for item in report.get("analysis_provenance", [])
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
  </section>

  <section>
    <h2>Lineage And Drug Resistance</h2>
    <div class="grid">
      {_metric_card("Validation status", lineage.get("status", "not_available"))}
      {_metric_card("Compared samples", lineage.get("compared_samples", "n/a"))}
      {_metric_card("Discordant samples", lineage.get("discordant_samples", "n/a"))}
      {_metric_card("Suppressed calls", lineage.get("suppressed_calls", "n/a"))}
    </div>
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

