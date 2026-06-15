import json
import logging
import os
import html as html_lib
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.routers.cases import _export_path, get_db

router = APIRouter(prefix="/cases", tags=["cases"])
logger = logging.getLogger(__name__)


# -- Case-specific comprehensive HTML report ----------------------------------

@router.get("/case-report/{case_id}", response_class=HTMLResponse)
def case_report_html(case_id: str, db: Session = Depends(get_db)):  # noqa: C901
    """
    Generate a comprehensive, self-contained HTML report for a single case.

    Includes: identity, genomic profile, drug-resistance, QC metrics,
    cluster membership, transmission context, related-case timeline,
    and case-level audit entries.
    """
    import datetime as _dt

    # -- Resolve case ----------------------------------------------------------
    pattern = f"{case_id}%" if len(case_id) < 36 else case_id
    core = db.execute(text("""
        SELECT
            c.pseudonymised_case_id::text  AS case_id,
            c.local_lab_sample_id,
            c.specimen_date,
            c.geographic_region,
            c.case_status,
            ti.lineage,
            ti.sublineage,
            ti.predicted_drug_resistance,
            ti.resistance_mutations,
            ti.interpretation_summary,
            cs.sequence,
            sqm.qc_status,
            sqm.coverage_breadth,
            sqm.mean_depth,
            sqm.contamination_flag,
            sqm.ambiguous_base_percent,
            cc.cluster_id::text            AS cluster_id,
            cl.snp_distance,
            cl.investigation_status        AS cluster_status,
            COUNT(cc2.sample_id) OVER (PARTITION BY cc.cluster_id) AS cluster_size
        FROM cases c
        LEFT JOIN tb_interpretation    ti  ON ti.sample_id  = c.pseudonymised_case_id
        LEFT JOIN consensus_sequences  cs  ON cs.sample_id  = c.pseudonymised_case_id
        LEFT JOIN sample_qc_metrics    sqm ON sqm.sample_id = c.pseudonymised_case_id
        LEFT JOIN case_clusters        cc  ON cc.sample_id  = c.pseudonymised_case_id
        LEFT JOIN clusters             cl  ON cl.cluster_id = cc.cluster_id
        LEFT JOIN case_clusters        cc2 ON cc2.cluster_id = cc.cluster_id
        WHERE CAST(c.pseudonymised_case_id AS TEXT) LIKE :pat
        LIMIT 1
    """), {"pat": pattern}).mappings().first()

    if not core:
        return HTMLResponse(
            content=f"<html><body><h2>Case not found: {html_lib.escape(case_id)}</h2></body></html>",
            status_code=404,
        )

    full_id   = core["case_id"]
    region    = core["geographic_region"] or "Unknown"
    short_id  = full_id[:8]
    generated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # -- Related cases in same cluster ----------------------------------------
    cluster_peers: list[dict] = []
    if core["cluster_id"]:
        peer_rows = db.execute(text("""
            SELECT
                c.pseudonymised_case_id::text AS case_id,
                c.specimen_date,
                c.geographic_region,
                c.case_status,
                ti.lineage,
                ti.predicted_drug_resistance
            FROM case_clusters cc
            JOIN cases c ON c.pseudonymised_case_id = cc.sample_id
            LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
            WHERE cc.cluster_id = CAST(:cid AS uuid)
            ORDER BY c.specimen_date
        """), {"cid": core["cluster_id"]}).mappings().all()
        cluster_peers = [
            {
                "case_id":    str(r["case_id"])[:8],
                "date":       str(r["specimen_date"]),
                "region":     r["geographic_region"],
                "status":     r["case_status"],
                "lineage":    r["lineage"],
                "resistance": r["predicted_drug_resistance"],
                "is_index":   r["case_id"] == full_id,
            }
            for r in peer_rows
        ]

    # -- Regional case history timeline ---------------------------------------
    history_rows = db.execute(text("""
        SELECT
            c.pseudonymised_case_id::text AS case_id,
            c.specimen_date,
            c.geographic_region,
            c.case_status,
            ti.lineage,
            ti.predicted_drug_resistance,
            cc.cluster_id::text AS cluster_id
        FROM cases c
        LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
        LEFT JOIN case_clusters cc     ON cc.sample_id = c.pseudonymised_case_id
        WHERE c.pseudonymised_case_id = CAST(:cid AS uuid)
           OR c.geographic_region = :region
        ORDER BY c.specimen_date
    """), {"cid": full_id, "region": region}).mappings().all()
    timeline = [
        {
            "case_id":    str(r["case_id"])[:8],
            "date":       str(r["specimen_date"]),
            "region":     r["geographic_region"],
            "status":     r["case_status"],
            "lineage":    r["lineage"],
            "resistance": r["predicted_drug_resistance"],
            "cluster_id": str(r["cluster_id"])[:8] if r["cluster_id"] else None,
            "is_index":   r["case_id"] == full_id,
        }
        for r in history_rows
    ]

    # -- Audit entries for this case -------------------------------------------
    audit_rows: list[dict] = []
    try:
        audit_rows = [
            dict(r) for r in db.execute(text("""
                SELECT action, user_id, timestamp, details
                FROM audit_log
                WHERE details ILIKE :pat
                ORDER BY timestamp DESC
                LIMIT 50
            """), {"pat": f"%{short_id}%"}).mappings().all()
        ]
    except Exception:
        audit_rows = []

    # -- Transmission network context -----------------------------------------
    tx_context: dict = {}
    tx_path = _export_path("transmission_network.json")
    if os.path.exists(tx_path):
        try:
            with open(tx_path, "r", encoding="utf-8") as _f:
                tx_data = json.load(_f)
            key_nodes = tx_data.get("key_nodes") or []
            edges     = tx_data.get("edges") or tx_data.get("transmission_edges") or []
            # find edges involving this case
            related_edges = [
                e for e in edges
                if short_id in str(e.get("from", "")) or short_id in str(e.get("to", ""))
                   or short_id in str(e.get("source", "")) or short_id in str(e.get("target", ""))
            ]
            node_match = next(
                (n for n in key_nodes if short_id in str(n.get("id", ""))), None
            )
            tx_context = {
                "is_key_node":    node_match is not None,
                "node_details":   node_match,
                "linked_edges":   related_edges[:20],
                "total_edges":    len(edges),
                "total_nodes":    len(key_nodes),
            }
        except Exception:
            tx_context = {}

    synthesis_pair_by_directed: dict[tuple[str, str], dict] = {}
    synthesis_pair_by_unordered: dict[tuple[str, str], dict] = {}
    synthesis_cluster_by_id: dict[str, dict] = {}
    synthesis_path = _export_path("synthesis_output.json")
    if os.path.exists(synthesis_path):
        try:
            with open(synthesis_path, "r", encoding="utf-8") as _f:
                synthesis_data = json.load(_f)
            for pair in synthesis_data.get("pairs") or []:
                if not isinstance(pair, dict):
                    continue
                source = str(pair.get("source") or "")
                target = str(pair.get("target") or "")
                if not source or not target:
                    continue
                synthesis_pair_by_directed[(source, target)] = pair
                synthesis_pair_by_unordered[tuple(sorted([source, target]))] = pair
            for cluster in synthesis_data.get("clusters") or []:
                if isinstance(cluster, dict) and cluster.get("cluster_id"):
                    synthesis_cluster_by_id[str(cluster.get("cluster_id"))] = cluster
        except Exception:
            synthesis_pair_by_directed = {}
            synthesis_pair_by_unordered = {}
            synthesis_cluster_by_id = {}

    # -- TBProfiler JSON artifact ----------------------------------------------
    tbp_data: dict | None = None
    tbp_dir = _export_path("tbprofiler")
    if os.path.isdir(tbp_dir):
        for fname in os.listdir(tbp_dir):
            if short_id in fname and fname.endswith(".json"):
                try:
                    with open(os.path.join(tbp_dir, fname), "r", encoding="utf-8") as _f:
                        tbp_data = json.load(_f)
                except Exception:
                    logger.warning("Failed to load TBProfiler JSON %s", fname, exc_info=True)
                break

    # -- Helper functions ------------------------------------------------------
    def _e(v) -> str:
        return html_lib.escape("" if v is None else str(v), quote=True)

    def _badge_status(status: str | None) -> str:
        s = (status or "").lower()
        colour = {"open": "#e63946", "closed": "#2a9d8f", "active": "#e63946",
                  "pass": "#2a9d8f", "fail": "#e63946", "passed": "#2a9d8f",
                  "failed": "#e63946"}.get(s, "#6c757d")
        return (f'<span style="background:{colour};color:#fff;padding:2px 8px;'
                f'border-radius:10px;font-size:0.8em;font-weight:600">{_e(status or "Unknown")}</span>')

    def _synthesis_badge(status: str | None) -> str:
        raw = status or "Not synthesised"
        s = raw.lower().replace("_", " ")
        if "strong" in s:
            colour = "#2a9d8f"
        elif "moderate" in s or "genomic only" in s:
            colour = "#f4a261"
        elif "model only" in s:
            colour = "#e76f51"
        elif "contradictory" in s:
            colour = "#e63946"
        else:
            colour = "#6c757d"
        return (
            f'<span style="background:{colour};color:#fff;padding:2px 8px;'
            f'border-radius:10px;font-size:0.8em;font-weight:600">{_e(s.title())}</span>'
        )

    def _synthesis_pair_for(source: str, target: str) -> dict:
        return (
            synthesis_pair_by_directed.get((source, target))
            or synthesis_pair_by_unordered.get(tuple(sorted([source, target])))
            or {}
        )

    def _cluster_synthesis_summary(cluster_id: str | None) -> dict:
        if not cluster_id:
            return {}
        cluster = synthesis_cluster_by_id.get(str(cluster_id)) or {}
        return cluster.get("summary") if isinstance(cluster, dict) else {}

    def _cluster_lineage_summary(cluster_id: str | None) -> str:
        if not cluster_id:
            return "-"
        cluster = synthesis_cluster_by_id.get(str(cluster_id)) or {}
        distribution = cluster.get("lineage_distribution") if isinstance(cluster, dict) else {}
        if not isinstance(distribution, dict) or not distribution:
            return "-"
        items = sorted(distribution.items(), key=lambda item: (-int(item[1] or 0), str(item[0])))
        return ", ".join(f"{key}: {value}" for key, value in items[:4])

    def _resistance_badge(dr) -> str:
        if not dr:
            return '<span style="color:#6c757d;font-style:italic">Not determined</span>'
        dr_str = str(dr)
        if any(x in dr_str.lower() for x in ["xdr", "extensively"]):
            colour = "#7b2d8b"
        elif any(x in dr_str.lower() for x in ["mdr", "multi"]):
            colour = "#e63946"
        elif any(x in dr_str.lower() for x in ['"r"', "'r'", ": r", ":r", "resistant"]):
            colour = "#f4a261"
        else:
            colour = "#2a9d8f"
        return (f'<span style="background:{colour};color:#fff;padding:2px 8px;'
                f'border-radius:10px;font-size:0.8em;font-weight:600">{_e(dr_str[:80])}</span>')

    def _metric_card(label: str, value: str, sub: str = "", alert: bool = False) -> str:
        border = "#e63946" if alert else "#2a9d8f"
        return (
            f'<div style="background:#fff;border-left:4px solid {border};border-radius:6px;'
            f'padding:14px 18px;min-width:140px;box-shadow:0 1px 4px rgba(0,0,0,.08)">'
            f'<div style="font-size:.75em;color:#6c757d;text-transform:uppercase;letter-spacing:.04em">{_e(label)}</div>'
            f'<div style="font-size:1.6em;font-weight:700;color:#212529;line-height:1.2">{value}</div>'
            f'{"<div style=font-size:.8em;color:#6c757d;margin-top:2px>" + _e(sub) + "</div>" if sub else ""}'
            f'</div>'
        )

    def _section(title: str, body: str) -> str:
        return (
            f'<section style="margin-bottom:32px">'
            f'<h2 style="font-size:1.1em;font-weight:700;color:#1d3557;border-bottom:2px solid #e9ecef;'
            f'padding-bottom:6px;margin-bottom:14px">{_e(title)}</h2>'
            f'{body}</section>'
        )

    def _kv_table(rows: list[tuple[str, str]]) -> str:
        tr = "".join(
            f'<tr><th style="width:220px;text-align:left;padding:6px 10px;color:#495057;'
            f'font-weight:600;background:#f8f9fa">{_e(k)}</th>'
            f'<td style="padding:6px 10px">{v}</td></tr>'
            for k, v in rows
        )
        return (
            '<table style="width:100%;border-collapse:collapse;border:1px solid #dee2e6;'
            'border-radius:4px;overflow:hidden"><tbody>' + tr + '</tbody></table>'
        )

    def _data_table(headers: list[str], rows: list[list[str]], empty: str = "No data") -> str:
        if not rows:
            return f'<p style="color:#6c757d;font-style:italic">{_e(empty)}</p>'
        th = "".join(
            f'<th style="padding:7px 10px;background:#1d3557;color:#fff;text-align:left;'
            f'font-weight:600;font-size:.85em">{_e(h)}</th>'
            for h in headers
        )
        tr_html = ""
        for i, row in enumerate(rows):
            bg = "#f8f9fa" if i % 2 else "#fff"
            tr_html += (
                '<tr style="background:' + bg + '">'
                + "".join(f'<td style="padding:6px 10px;font-size:.875em;border-top:1px solid #dee2e6">{c}</td>' for c in row)
                + "</tr>"
            )
        return (
            '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">'
            '<thead><tr>' + th + '</tr></thead><tbody>' + tr_html + '</tbody></table></div>'
        )

    # -- Build report sections -------------------------------------------------

    # 1. Identity card
    identity_body = _kv_table([
        ("Pseudonymised Case ID",    _e(full_id)),
        ("Short Reference",          _e(short_id)),
        ("Local Lab Sample ID",      _e(core["local_lab_sample_id"] or "-")),
        ("Specimen Date",            _e(core["specimen_date"])),
        ("Geographic Region",        _e(region)),
        ("Case Status",              _badge_status(core["case_status"])),
        ("Report Generated",         _e(generated)),
    ])
    identity_sec = _section("1. Case Identity", identity_body)

    # 2. Genomic / Lineage profile
    lineage_body = _kv_table([
        ("Lineage",          _e(core["lineage"] or "Not determined")),
        ("Sublineage",       _e(core["sublineage"] or "-")),
        ("Sequence Present", _e("Yes" if core["sequence"] else "No")),
        ("Interpretation",   _e(core["interpretation_summary"] or "-")),
    ])
    lineage_sec = _section("2. Genomic &amp; Lineage Profile", lineage_body)

    # 3. Drug resistance
    dr_raw = core["predicted_drug_resistance"]
    if isinstance(dr_raw, dict):
        dr_rows: list[list[str]] = [
            [_e(drug), _badge_status(result)]
            for drug, result in dr_raw.items()
        ]
        dr_table = _data_table(["Drug", "Predicted Result"], dr_rows, "No resistance data")
    else:
        dr_table = f'<p>{_resistance_badge(dr_raw)}</p>'

    mut_raw = core["resistance_mutations"]
    if isinstance(mut_raw, list) and mut_raw:
        mut_table = _data_table(
            ["Gene / Mutation", "Drug", "Confidence"],
            [[_e(str(m.get("mutation", m) if isinstance(m, dict) else m)),
              _e(str(m.get("drug", "-") if isinstance(m, dict) else "-")),
              _e(str(m.get("confidence", "-") if isinstance(m, dict) else "-"))]
             for m in mut_raw[:30]],
            "No mutation data"
        )
    elif mut_raw:
        mut_table = f'<pre style="font-size:.8em">{_e(str(mut_raw)[:1000])}</pre>'
    else:
        mut_table = '<p style="color:#6c757d;font-style:italic">No resistance mutations recorded</p>'

    dr_sec = _section(
        "3. Drug Resistance Profile",
        '<h3 style="font-size:.95em;margin:0 0 8px;color:#495057">Predicted Resistance</h3>'
        + dr_table
        + '<h3 style="font-size:.95em;margin:16px 0 8px;color:#495057">Resistance Mutations</h3>'
        + mut_table,
    )

    # 4. QC metrics
    qc_alert = (
        str(core.get("qc_status", "")).lower() in {"fail", "failed"}
        or bool(core.get("contamination_flag"))
    )
    qc_body = _kv_table([
        ("QC Status",             _badge_status(core["qc_status"])),
        ("Coverage Breadth",      _e(f"{core['coverage_breadth']:.1f}%" if core["coverage_breadth"] is not None else "-")),
        ("Mean Depth",            _e(f"{core['mean_depth']:.1f}x" if core["mean_depth"] is not None else "-")),
        ("Contamination Flag",    _e("[WARN] YES" if core["contamination_flag"] else "No")),
        ("Ambiguous Bases (%)",   _e(f"{core['ambiguous_base_percent']:.2f}%" if core["ambiguous_base_percent"] is not None else "-")),
    ])
    qc_sec = _section(
        "4. Sequencing QC Metrics" + (" [WARN]" if qc_alert else ""),
        qc_body,
    )

    # 5. Cluster membership
    if core["cluster_id"]:
        cluster_synthesis_summary = _cluster_synthesis_summary(core["cluster_id"])
        cluster_tx = cluster_synthesis_summary.get("transmission_generations") if isinstance(cluster_synthesis_summary, dict) else {}
        max_generation = cluster_tx.get("max_generation") if isinstance(cluster_tx, dict) else None
        sustained_flag = cluster_tx.get("sustained_transmission_flag") if isinstance(cluster_tx, dict) else None
        cluster_body = _kv_table([
            ("Cluster ID",           _e(core["cluster_id"][:8])),
            ("Cluster Size",         _e(str(core["cluster_size"]))),
            ("SNP Distance (max)",   _e(str(core["snp_distance"]) if core["snp_distance"] is not None else "-")),
            ("Investigation Status", _badge_status(core["cluster_status"])),
            ("Lineage distribution", _e(_cluster_lineage_summary(core["cluster_id"]))),
            ("Sustained transmission", _e("Yes" if sustained_flag else ("No" if sustained_flag is not None else "-"))),
            ("Maximum generation", _e(str(max_generation) if max_generation is not None else "-")),
        ])
        peer_table = _data_table(
            ["Case ID", "Date", "Region", "Lineage", "Resistance", "Status"],
            [
                [
                    '<strong>' + _e(p["case_id"]) + '</strong>' if p["is_index"] else _e(p["case_id"]),
                    _e(p["date"]),
                    _e(p["region"]),
                    _e(p["lineage"] or "-"),
                    _resistance_badge(p["resistance"]),
                    _badge_status(p["status"]),
                ]
                for p in cluster_peers
            ],
            "No cluster peers found",
        )
        cluster_sec = _section(
            "5. Cluster Membership",
            cluster_body
            + '<h3 style="font-size:.95em;margin:16px 0 8px;color:#495057">Cluster Members</h3>'
            + peer_table,
        )
    else:
        cluster_sec = _section(
            "5. Cluster Membership",
            '<p style="color:#6c757d;font-style:italic">This case is not assigned to any cluster.</p>',
        )

    # 6. Transmission network context
    if tx_context:
        tx_rows = _kv_table([
            ("Is Key Network Node",      _e("Yes" if tx_context.get("is_key_node") else "No")),
            ("Linked Transmission Edges", _e(str(len(tx_context.get("linked_edges", []))))),
            ("Total Network Edges",       _e(str(tx_context.get("total_edges", "-")))),
            ("Total Key Nodes",           _e(str(tx_context.get("total_nodes", "-")))),
        ])
        edge_data = tx_context.get("linked_edges", [])
        if edge_data:
            edge_table = _data_table(
                ["From", "To", "Probability / Weight", "Synthesis confidence", "Priority", "Lineage / DR", "Flags"],
                [
                    (lambda src, tgt, syn: [
                        _e(src[:10]),
                        _e(tgt[:10]),
                        _e(str(e.get("probability", e.get("weight", "-")))),
                        _synthesis_badge(str(syn.get("confidence") or syn.get("confidence_code") or "Not synthesised")),
                        _e(str(syn.get("priority_score", "n/a"))),
                        _e(
                            " / ".join(
                                part for part in [
                                    str(syn.get("lineage_concordance") or ""),
                                    str(syn.get("resistance_profile_concordance") or ""),
                                ]
                                if part
                            )
                            or "n/a"
                        ),
                        _e(", ".join(str(flag).replace("_", " ") for flag in (syn.get("flags") or [])[:3]) or "none"),
                    ])(
                        str(e.get("from", e.get("source", "-"))),
                        str(e.get("to", e.get("target", "-"))),
                        _synthesis_pair_for(
                            str(e.get("from", e.get("source", ""))),
                            str(e.get("to", e.get("target", ""))),
                        ),
                    )
                    for e in edge_data
                ],
                "No linked edges",
            )
        else:
            edge_table = '<p style="color:#6c757d;font-style:italic">No direct transmission edges found for this case.</p>'
        tx_sec = _section(
            "6. Transmission Network Context",
            tx_rows
            + '<h3 style="font-size:.95em;margin:16px 0 8px;color:#495057">Linked Edges</h3>'
            + edge_table,
        )
    else:
        tx_sec = _section(
            "6. Transmission Network Context",
            '<p style="color:#6c757d;font-style:italic">No transmission network data available. Run the outbreaker2 analysis first.</p>',
        )

    # 7. Regional case timeline
    timeline_sec = _section(
        "7. Regional Case Timeline",
        _data_table(
            ["Case ID", "Date", "Region", "Lineage", "Resistance", "Cluster", "Status"],
            [
                [
                    '<strong>' + _e(t["case_id"]) + '</strong>' if t["is_index"] else _e(t["case_id"]),
                    _e(t["date"]),
                    _e(t["region"]),
                    _e(t["lineage"] or "-"),
                    _resistance_badge(t["resistance"]),
                    _e(t["cluster_id"] or "-"),
                    _badge_status(t["status"]),
                ]
                for t in timeline
            ],
            "No timeline data",
        ),
    )

    # 8. TBProfiler data
    if tbp_data and isinstance(tbp_data, dict):
        tbp_fields = [
            ("TBProfiler Version",    _e(tbp_data.get("tbprofiler_version", "-"))),
            ("Main Lineage",          _e(tbp_data.get("main_lin", "-"))),
            ("Sub Lineage",           _e(tbp_data.get("sub_lin", "-"))),
            ("DR Type",               _e(tbp_data.get("drtype", "-"))),
            ("Median Coverage",       _e(str(tbp_data.get("median_coverage", "-")))),
            ("Pct Reads Mapped",      _e(str(tbp_data.get("pct_reads_mapped", "-")))),
        ]
        tbp_sec = _section("8. TBProfiler Analysis Details", _kv_table(tbp_fields))
    else:
        tbp_sec = _section(
            "8. TBProfiler Analysis Details",
            '<p style="color:#6c757d;font-style:italic">No TBProfiler output found for this case.</p>',
        )

    # 9. Audit trail
    if audit_rows:
        audit_sec = _section(
            "9. Case Audit Trail",
            _data_table(
                ["Timestamp", "Action", "User", "Details"],
                [
                    [
                        _e(str(a.get("timestamp", "-"))[:19]),
                        _e(str(a.get("action", "-"))),
                        _e(str(a.get("user_id", "-"))),
                        _e(str(a.get("details", ""))[:120]),
                    ]
                    for a in audit_rows
                ],
                "No audit entries",
            ),
        )
    else:
        audit_sec = _section(
            "9. Case Audit Trail",
            '<p style="color:#6c757d;font-style:italic">No audit entries found referencing this case.</p>',
        )

    # -- Assemble HTML ---------------------------------------------------------
    dr_summary_text = _e(str(core["predicted_drug_resistance"])[:60]) if core["predicted_drug_resistance"] else "Not determined"
    lineage_text    = _e(core["lineage"] or "Unknown")
    status_badge    = _badge_status(core["case_status"])

    metric_strip = (
        '<div style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:28px">'
        + _metric_card("Case ID",     short_id)
        + _metric_card("Region",      region)
        + _metric_card("Lineage",     core["lineage"] or "-")
        + _metric_card("Cluster",     core["cluster_id"][:8] if core["cluster_id"] else "None",
                        sub=f"{core['cluster_size']} members" if core["cluster_id"] else "")
        + _metric_card("QC Status",   core["qc_status"] or "-",
                        alert=qc_alert)
        + _metric_card("Timeline Cases", str(len(timeline)))
        + '</div>'
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Case Report - {_e(short_id)}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f0f4f8; color: #212529; font-size: 14px; line-height: 1.5;
  }}
  .report-header {{
    background: linear-gradient(135deg, #1d3557 0%, #457b9d 100%);
    color: #fff; padding: 28px 40px 20px;
  }}
  .report-header h1 {{ margin: 0 0 4px; font-size: 1.6em; }}
  .report-header p  {{ margin: 0; opacity: .8; font-size: .9em; }}
  .report-body {{
    max-width: 1100px; margin: 28px auto; padding: 0 24px;
  }}
  .data-card {{
    background: #fff; border-radius: 8px; box-shadow: 0 1px 6px rgba(0,0,0,.09);
    padding: 28px 32px; margin-bottom: 24px;
  }}
  @media print {{
    body {{ background: #fff; }}
    .report-header {{ background: #1d3557 !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
    .data-card {{ box-shadow: none; border: 1px solid #dee2e6; }}
    .no-print {{ display: none; }}
  }}
</style>
</head>
<body>
<div class="report-header">
  <h1>TB Case Investigation Report</h1>
  <p>Case {_e(short_id)} &nbsp; | &nbsp; {_e(region)} &nbsp; | &nbsp;
     Status: {status_badge} &nbsp; | &nbsp; Generated {_e(generated)}</p>
</div>
<div class="report-body">
  <div class="no-print" style="margin-bottom:18px">
    <button onclick="window.print()"
      style="padding:8px 20px;background:#1d3557;color:#fff;border:none;border-radius:4px;
             cursor:pointer;font-size:.9em;margin-right:8px">
      &#x1F5B6; Print / Save as PDF
    </button>
    <button onclick="window.close()"
      style="padding:8px 20px;background:#6c757d;color:#fff;border:none;border-radius:4px;
             cursor:pointer;font-size:.9em">
      Close
    </button>
  </div>
  {metric_strip}
  <div class="data-card">
    {identity_sec}
    {lineage_sec}
    {dr_sec}
    {qc_sec}
    {cluster_sec}
    {tx_sec}
    {timeline_sec}
    {tbp_sec}
    {audit_sec}
  </div>
  <p style="text-align:center;color:#adb5bd;font-size:.8em;margin-top:24px">
    TB Genomic Surveillance Platform &nbsp; | &nbsp; Confidential &nbsp; | &nbsp;
    For authorised public-health use only
  </p>
</div>
</body>
</html>"""

    return HTMLResponse(content=html)

