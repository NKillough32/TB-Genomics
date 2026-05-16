import json
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.snp_validation import validated_snp_distance
from backend.synthesis.transmission_synthesis import (
    SynthesisConfig,
    build_cluster_risk_summary,
    build_transmission_synthesis,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])


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
               cs.sequence
        FROM cases c
        LEFT JOIN case_clusters cc ON cc.sample_id = c.pseudonymised_case_id
        LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
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
