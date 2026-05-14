"""
Cluster Investigation Centre router.

Workflow:
  cluster detected → risk score computed → assigned to reviewer
  → epi fields reviewed → actions recorded → decision signed off
  → report generated
"""

import html as html_lib
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cluster-investigations", tags=["cluster-investigations"])


# ── DB helpers ─────────────────────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_tables(db: Session) -> None:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS cluster_investigations (
            investigation_id  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            cluster_id        UUID NOT NULL REFERENCES clusters(cluster_id) ON DELETE CASCADE,
            risk_score        NUMERIC NOT NULL DEFAULT 0,
            risk_band         TEXT NOT NULL DEFAULT 'low',
            assigned_to       TEXT,
            status            TEXT NOT NULL DEFAULT 'open',
            epi_notes         TEXT,
            actions           JSONB NOT NULL DEFAULT '[]',
            decision          TEXT,
            decision_by       TEXT,
            decision_at       TIMESTAMP,
            created_at        TIMESTAMP DEFAULT NOW(),
            updated_at        TIMESTAMP DEFAULT NOW()
        )
    """))
    db.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_cluster_investigations_cluster
            ON cluster_investigations (cluster_id)
    """))
    db.commit()


# ── Risk scoring ───────────────────────────────────────────────────────────────

def _risk_band(score: float) -> str:
    if score >= 66:
        return "critical"
    if score >= 41:
        return "high"
    if score >= 21:
        return "medium"
    return "low"


def _compute_risk_score(db: Session, cluster_id: str) -> dict:
    """
    Derive a risk score for a cluster from live DB data.

    Components
    ----------
    Cluster size          : 2 pts per case (capped at 40)
    Alert flag            : +20
    Multi-region spread   : +10 per additional region beyond the first
    Recent specimen (90d) : +15
    MDR / resistance flag : +25
    """
    score = 0.0
    components: dict[str, float] = {}

    try:
        # Cluster size
        size_row = db.execute(text("""
            SELECT COUNT(*)::int AS n
            FROM case_clusters cc
            WHERE cc.cluster_id = CAST(:cid AS UUID)
        """), {"cid": cluster_id}).mappings().first()
        size = int(size_row["n"] or 0) if size_row else 0
        size_pts = min(size * 2, 40)
        score += size_pts
        components["cluster_size"] = size_pts

        # Alert flag
        alert_row = db.execute(text("""
            SELECT alert_flag FROM clusters WHERE cluster_id = CAST(:cid AS UUID)
        """), {"cid": cluster_id}).mappings().first()
        alert = bool(alert_row["alert_flag"]) if alert_row else False
        alert_pts = 20.0 if alert else 0.0
        score += alert_pts
        components["alert_flag"] = alert_pts

        # Geographic spread
        region_row = db.execute(text("""
            SELECT COUNT(DISTINCT c.geographic_region)::int AS n_regions
            FROM case_clusters cc
            JOIN cases c ON c.pseudonymised_case_id = cc.sample_id
            WHERE cc.cluster_id = CAST(:cid AS UUID)
        """), {"cid": cluster_id}).mappings().first()
        n_regions = int(region_row["n_regions"] or 1) if region_row else 1
        spread_pts = max(0, (n_regions - 1) * 10)
        score += spread_pts
        components["geographic_spread"] = spread_pts

        # Recency
        recent_row = db.execute(text("""
            SELECT COUNT(*)::int AS n
            FROM case_clusters cc
            JOIN cases c ON c.pseudonymised_case_id = cc.sample_id
            WHERE cc.cluster_id = CAST(:cid AS UUID)
              AND c.specimen_date >= NOW() - INTERVAL '90 days'
        """), {"cid": cluster_id}).mappings().first()
        recent = int(recent_row["n"] or 0) if recent_row else 0
        recent_pts = 15.0 if recent > 0 else 0.0
        score += recent_pts
        components["recent_cases"] = recent_pts

        # Resistance / MDR signal
        mdr_row = db.execute(text("""
            SELECT COUNT(*)::int AS n
            FROM case_clusters cc
            JOIN tb_interpretation ti ON ti.sample_id = cc.sample_id
            WHERE cc.cluster_id = CAST(:cid AS UUID)
              AND ti.predicted_drug_resistance IS NOT NULL
              AND ti.predicted_drug_resistance::text NOT IN ('null', '{}', '[]')
        """), {"cid": cluster_id}).mappings().first()
        has_resistance = int(mdr_row["n"] or 0) > 0 if mdr_row else False
        resistance_pts = 25.0 if has_resistance else 0.0
        score += resistance_pts
        components["resistance_signal"] = resistance_pts

    except Exception as exc:
        logger.warning("Risk score computation error for %s: %s", cluster_id, exc)

    band = _risk_band(score)
    return {"score": round(score, 1), "band": band, "components": components}


# ── Upsert investigation row ────────────────────────────────────────────────────

def _upsert_investigation(db: Session, cluster_id: str) -> None:
    """Ensure an investigation row exists; recompute risk score on every call."""
    _ensure_tables(db)
    risk = _compute_risk_score(db, cluster_id)
    db.execute(text("""
        INSERT INTO cluster_investigations
            (investigation_id, cluster_id, risk_score, risk_band, created_at, updated_at)
        VALUES
            (uuid_generate_v4(), CAST(:cid AS UUID), :score, :band, NOW(), NOW())
        ON CONFLICT (cluster_id)
        DO UPDATE SET
            risk_score = EXCLUDED.risk_score,
            risk_band  = EXCLUDED.risk_band,
            updated_at = NOW()
    """), {"cid": cluster_id, "score": risk["score"], "band": risk["band"]})
    db.commit()


# ── Pydantic models ─────────────────────────────────────────────────────────────

class AssignRequest(BaseModel):
    assigned_to: str


class EpiNotesRequest(BaseModel):
    epi_notes: str


class ActionRequest(BaseModel):
    action_type: str    # e.g. "contact_tracing", "interview", "referral", "notification"
    description: str
    performed_by: str
    performed_at: str | None = None  # ISO date string; defaults to now


class SignOffRequest(BaseModel):
    decision: str       # e.g. "no_further_action", "escalated", "public_health_response"
    decision_by: str
    notes: str | None = None


# ── Routes ──────────────────────────────────────────────────────────────────────

@router.get("")
def list_investigations(db: Session = Depends(get_db)):
    """
    Return all clusters with their investigation state and risk scores.
    Creates investigation rows on first call.
    """
    _ensure_tables(db)

    try:
        cluster_rows = db.execute(text("""
            SELECT cluster_id::text, snp_distance, investigation_status, alert_flag
            FROM clusters
            ORDER BY cluster_id
        """)).mappings().all()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    results = []
    for row in cluster_rows:
        cid = row["cluster_id"]
        _upsert_investigation(db, cid)

        inv = db.execute(text("""
            SELECT investigation_id::text, risk_score, risk_band, assigned_to,
                   status, epi_notes, actions, decision, decision_by, decision_at,
                   created_at, updated_at
            FROM cluster_investigations
            WHERE cluster_id = CAST(:cid AS UUID)
        """), {"cid": cid}).mappings().first()

        size_row = db.execute(text("""
            SELECT COUNT(*)::int AS n FROM case_clusters
            WHERE cluster_id = CAST(:cid AS UUID)
        """), {"cid": cid}).mappings().first()

        results.append({
            "cluster_id": cid,
            "snp_distance": row["snp_distance"],
            "cluster_investigation_status": row["investigation_status"],
            "alert_flag": bool(row["alert_flag"]),
            "case_count": int(size_row["n"] or 0) if size_row else 0,
            "investigation_id": inv["investigation_id"] if inv else None,
            "risk_score": float(inv["risk_score"] or 0) if inv else 0.0,
            "risk_band": inv["risk_band"] if inv else "low",
            "assigned_to": inv["assigned_to"] if inv else None,
            "status": inv["status"] if inv else "open",
            "has_epi_notes": bool(inv["epi_notes"]) if inv else False,
            "action_count": len(inv["actions"] or []) if inv else 0,
            "decision": inv["decision"] if inv else None,
            "decision_by": inv["decision_by"] if inv else None,
            "decision_at": inv["decision_at"].isoformat() if inv and inv["decision_at"] else None,
            "updated_at": inv["updated_at"].isoformat() if inv and inv["updated_at"] else None,
        })

    results.sort(key=lambda x: (-x["risk_score"], x["cluster_id"]))
    return {"investigations": results, "total": len(results)}


@router.get("/{cluster_id}")
def get_investigation(cluster_id: str, db: Session = Depends(get_db)):
    """Return full investigation detail for a single cluster."""
    _upsert_investigation(db, cluster_id)

    inv = db.execute(text("""
        SELECT investigation_id::text, cluster_id::text, risk_score, risk_band,
               assigned_to, status, epi_notes, actions,
               decision, decision_by, decision_at, created_at, updated_at
        FROM cluster_investigations
        WHERE cluster_id = CAST(:cid AS UUID)
    """), {"cid": cluster_id}).mappings().first()

    if not inv:
        raise HTTPException(status_code=404, detail="Investigation not found")

    risk = _compute_risk_score(db, cluster_id)

    # Cluster members
    members = db.execute(text("""
        SELECT c.pseudonymised_case_id::text AS case_id,
               c.specimen_date, c.geographic_region,
               c.case_status,
               ti.lineage, ti.predicted_drug_resistance
        FROM case_clusters cc
        JOIN cases c ON c.pseudonymised_case_id = cc.sample_id
        LEFT JOIN tb_interpretation ti ON ti.sample_id = cc.sample_id
        WHERE cc.cluster_id = CAST(:cid AS UUID)
        ORDER BY c.specimen_date DESC NULLS LAST
    """), {"cid": cluster_id}).mappings().all()

    return {
        "cluster_id": cluster_id,
        "investigation_id": inv["investigation_id"],
        "risk_score": float(inv["risk_score"] or 0),
        "risk_band": inv["risk_band"],
        "risk_components": risk["components"],
        "assigned_to": inv["assigned_to"],
        "status": inv["status"],
        "epi_notes": inv["epi_notes"],
        "actions": inv["actions"] or [],
        "decision": inv["decision"],
        "decision_by": inv["decision_by"],
        "decision_at": inv["decision_at"].isoformat() if inv["decision_at"] else None,
        "created_at": inv["created_at"].isoformat() if inv["created_at"] else None,
        "updated_at": inv["updated_at"].isoformat() if inv["updated_at"] else None,
        "members": [
            {
                "case_id": m["case_id"],
                "specimen_date": m["specimen_date"].isoformat() if m["specimen_date"] else None,
                "region": m["geographic_region"],
                "status": m["case_status"],
                "lineage": m["lineage"],
                "resistance": m["predicted_drug_resistance"],
            }
            for m in members
        ],
    }


@router.post("/{cluster_id}/assign")
def assign_reviewer(cluster_id: str, body: AssignRequest, db: Session = Depends(get_db)):
    """Assign an investigation to a reviewer and move status to under_review."""
    _upsert_investigation(db, cluster_id)
    if not body.assigned_to.strip():
        raise HTTPException(status_code=422, detail="assigned_to must not be empty")

    db.execute(text("""
        UPDATE cluster_investigations
        SET assigned_to = :reviewer,
            status      = CASE WHEN status = 'open' THEN 'under_review' ELSE status END,
            updated_at  = NOW()
        WHERE cluster_id = CAST(:cid AS UUID)
    """), {"reviewer": body.assigned_to.strip(), "cid": cluster_id})
    db.commit()

    # Audit
    db.execute(text("""
        INSERT INTO audit_log (action, user_id, details, timestamp)
        VALUES ('cluster_investigation_assigned', :reviewer,
                jsonb_build_object('cluster_id', :cid, 'assigned_to', :reviewer), NOW())
    """), {"reviewer": body.assigned_to.strip(), "cid": cluster_id})
    db.commit()

    return {"ok": True, "assigned_to": body.assigned_to.strip()}


@router.put("/{cluster_id}/epi-notes")
def update_epi_notes(cluster_id: str, body: EpiNotesRequest, db: Session = Depends(get_db)):
    """Save epidemiology review notes for a cluster investigation."""
    _upsert_investigation(db, cluster_id)

    db.execute(text("""
        UPDATE cluster_investigations
        SET epi_notes  = :notes,
            updated_at = NOW()
        WHERE cluster_id = CAST(:cid AS UUID)
    """), {"notes": body.epi_notes, "cid": cluster_id})
    db.commit()
    return {"ok": True}


@router.post("/{cluster_id}/actions")
def record_action(cluster_id: str, body: ActionRequest, db: Session = Depends(get_db)):
    """Append a public health action to the investigation log."""
    _upsert_investigation(db, cluster_id)

    performed_at = body.performed_at or datetime.now(timezone.utc).isoformat()
    new_action = {
        "id": str(uuid.uuid4()),
        "action_type": body.action_type.strip(),
        "description": body.description.strip(),
        "performed_by": body.performed_by.strip(),
        "performed_at": performed_at,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

    db.execute(text("""
        UPDATE cluster_investigations
        SET actions    = actions || CAST(:action AS JSONB),
            updated_at = NOW()
        WHERE cluster_id = CAST(:cid AS UUID)
    """), {"action": json.dumps(new_action), "cid": cluster_id})
    db.commit()

    # Audit
    db.execute(text("""
        INSERT INTO audit_log (action, user_id, details, timestamp)
        VALUES ('cluster_investigation_action', :user,
                jsonb_build_object('cluster_id', :cid,
                                   'action_type', :atype,
                                   'description', :desc), NOW())
    """), {
        "user": body.performed_by.strip(),
        "cid": cluster_id,
        "atype": body.action_type.strip(),
        "desc": body.description.strip(),
    })
    db.commit()

    return {"ok": True, "action_id": new_action["id"]}


@router.post("/{cluster_id}/sign-off")
def sign_off(cluster_id: str, body: SignOffRequest, db: Session = Depends(get_db)):
    """Record a formal sign-off decision for a cluster investigation."""
    _upsert_investigation(db, cluster_id)
    if not body.decision.strip():
        raise HTTPException(status_code=422, detail="decision must not be empty")
    if not body.decision_by.strip():
        raise HTTPException(status_code=422, detail="decision_by must not be empty")

    db.execute(text("""
        UPDATE cluster_investigations
        SET decision    = :decision,
            decision_by = :by,
            decision_at = NOW(),
            status      = 'signed_off',
            epi_notes   = CASE
                            WHEN :notes IS NOT NULL AND TRIM(:notes) <> ''
                            THEN COALESCE(epi_notes || E'\n\n[Sign-off notes] ', '') || :notes
                            ELSE epi_notes
                          END,
            updated_at  = NOW()
        WHERE cluster_id = CAST(:cid AS UUID)
    """), {
        "decision": body.decision.strip(),
        "by": body.decision_by.strip(),
        "notes": body.notes or "",
        "cid": cluster_id,
    })
    db.commit()

    # Audit
    db.execute(text("""
        INSERT INTO audit_log (action, user_id, details, timestamp)
        VALUES ('cluster_investigation_signed_off', :by,
                jsonb_build_object('cluster_id', :cid, 'decision', :decision), NOW())
    """), {"by": body.decision_by.strip(), "cid": cluster_id, "decision": body.decision.strip()})
    db.commit()

    return {"ok": True, "decision": body.decision.strip(), "decision_by": body.decision_by.strip()}


@router.get("/{cluster_id}/report", response_class=HTMLResponse)
def generate_report(cluster_id: str, db: Session = Depends(get_db)):
    """Generate a self-contained HTML investigation report for a cluster."""
    _upsert_investigation(db, cluster_id)

    inv = db.execute(text("""
        SELECT investigation_id::text, risk_score, risk_band, assigned_to,
               status, epi_notes, actions, decision, decision_by, decision_at,
               created_at, updated_at
        FROM cluster_investigations
        WHERE cluster_id = CAST(:cid AS UUID)
    """), {"cid": cluster_id}).mappings().first()

    if not inv:
        raise HTTPException(status_code=404, detail="Investigation not found")

    members = db.execute(text("""
        SELECT c.pseudonymised_case_id::text AS case_id,
               c.specimen_date, c.geographic_region, c.case_status,
               ti.lineage, ti.predicted_drug_resistance
        FROM case_clusters cc
        JOIN cases c ON c.pseudonymised_case_id = cc.sample_id
        LEFT JOIN tb_interpretation ti ON ti.sample_id = cc.sample_id
        WHERE cc.cluster_id = CAST(:cid AS UUID)
        ORDER BY c.specimen_date DESC NULLS LAST
    """), {"cid": cluster_id}).mappings().all()

    risk = _compute_risk_score(db, cluster_id)
    h = html_lib.escape
    actions: list[dict] = inv["actions"] or []
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    band_colour = {
        "critical": "#b91c1c",
        "high": "#c2410c",
        "medium": "#b45309",
        "low": "#15803d",
    }.get(inv["risk_band"] or "low", "#374151")

    # ── Member rows ─────────────────────────────────────────────────────────────
    member_rows = ""
    for m in members:
        dr_raw = m["predicted_drug_resistance"]
        if isinstance(dr_raw, dict):
            dr = ", ".join(dr_raw.keys()) or "none"
        elif isinstance(dr_raw, str) and dr_raw not in ("null", "{}"):
            dr = dr_raw
        else:
            dr = "none"
        member_rows += (
            f"<tr><td>{h(str(m['case_id'])[:8])}</td>"
            f"<td>{h(str(m['specimen_date'] or ''))}</td>"
            f"<td>{h(str(m['geographic_region'] or ''))}</td>"
            f"<td>{h(str(m['lineage'] or ''))}</td>"
            f"<td>{h(dr)}</td></tr>\n"
        )

    # ── Action rows ──────────────────────────────────────────────────────────────
    action_rows = ""
    for a in actions:
        action_rows += (
            f"<tr><td>{h(a.get('action_type',''))}</td>"
            f"<td>{h(a.get('description',''))}</td>"
            f"<td>{h(a.get('performed_by',''))}</td>"
            f"<td>{h(str(a.get('performed_at',''))[:10])}</td></tr>\n"
        )
    if not action_rows:
        action_rows = "<tr><td colspan='4'>No actions recorded</td></tr>\n"

    # ── Risk component table ─────────────────────────────────────────────────────
    component_rows = "".join(
        f"<tr><td>{h(k.replace('_', ' ').title())}</td><td>{h(str(v))}</td></tr>\n"
        for k, v in risk["components"].items()
    )

    decision_block = ""
    if inv["decision"]:
        decision_block = f"""
        <section>
          <h2>Sign-off Decision</h2>
          <p><strong>Decision:</strong> {h(inv['decision'])}</p>
          <p><strong>Signed off by:</strong> {h(str(inv['decision_by'] or ''))}</p>
          <p><strong>Date:</strong> {h(str(inv['decision_at'])[:19] if inv['decision_at'] else '')}</p>
        </section>"""

    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>Cluster Investigation Report — {h(cluster_id[:8])}</title>
<style>
  body{{font-family:system-ui,sans-serif;margin:0;padding:2rem;color:#111;background:#f9fafb;}}
  header{{background:#1e3a5f;color:#fff;padding:1.5rem 2rem;border-radius:8px;margin-bottom:1.5rem;}}
  header h1{{margin:0;font-size:1.4rem;}}
  header p{{margin:.3rem 0 0;opacity:.8;font-size:.9rem;}}
  section{{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:1.25rem 1.5rem;margin-bottom:1rem;}}
  h2{{margin:0 0 .75rem;font-size:1rem;color:#1e3a5f;border-bottom:1px solid #e5e7eb;padding-bottom:.4rem;}}
  table{{width:100%;border-collapse:collapse;font-size:.85rem;}}
  th{{background:#f3f4f6;text-align:left;padding:.4rem .6rem;font-weight:600;}}
  td{{padding:.35rem .6rem;border-top:1px solid #f3f4f6;}}
  .risk-badge{{display:inline-block;padding:.25rem .75rem;border-radius:999px;font-weight:700;
               color:#fff;background:{band_colour};font-size:1rem;}}
  .score{{font-size:2rem;font-weight:800;color:{band_colour};}}
  .kv{{display:grid;grid-template-columns:max-content 1fr;gap:.25rem .75rem;font-size:.9rem;}}
  .kv dt{{font-weight:600;color:#374151;}}
  .kv dd{{margin:0;color:#111;}}
  pre{{background:#f3f4f6;padding:.75rem;border-radius:6px;font-size:.82rem;white-space:pre-wrap;}}
  .footer{{color:#6b7280;font-size:.8rem;text-align:center;margin-top:1.5rem;}}
  @media print{{body{{background:#fff;padding:1rem;}}header{{border-radius:0;}}}}
</style>
</head>
<body>
<header>
  <h1>Cluster Investigation Report</h1>
  <p>Cluster ID: {h(cluster_id)} &nbsp;|&nbsp; Generated: {h(generated_at)}</p>
</header>

<section>
  <h2>Risk Assessment</h2>
  <p class="score">{h(str(risk['score']))}</p>
  <span class="risk-badge">{h((inv['risk_band'] or 'low').upper())}</span>
  <table style="margin-top:.75rem;max-width:400px">
    <tr><th>Component</th><th>Points</th></tr>
    {component_rows}
    <tr style="font-weight:700"><td>Total</td><td>{h(str(risk['score']))}</td></tr>
  </table>
</section>

<section>
  <h2>Investigation Status</h2>
  <dl class="kv">
    <dt>Status</dt><dd>{h(str(inv['status'] or '').replace('_', ' ').title())}</dd>
    <dt>Assigned to</dt><dd>{h(str(inv['assigned_to'] or 'Unassigned'))}</dd>
    <dt>Opened</dt><dd>{h(str(inv['created_at'])[:19] if inv['created_at'] else '')}</dd>
    <dt>Last updated</dt><dd>{h(str(inv['updated_at'])[:19] if inv['updated_at'] else '')}</dd>
  </dl>
</section>

<section>
  <h2>Cluster Members ({h(str(len(members)))} cases)</h2>
  <table>
    <tr><th>Case ID</th><th>Specimen Date</th><th>Region</th><th>Lineage</th><th>Resistance</th></tr>
    {member_rows if member_rows else "<tr><td colspan='5'>No members found</td></tr>"}
  </table>
</section>

<section>
  <h2>Epidemiology Notes</h2>
  <pre>{h(str(inv['epi_notes'] or 'No notes recorded.'))}</pre>
</section>

<section>
  <h2>Recorded Actions</h2>
  <table>
    <tr><th>Type</th><th>Description</th><th>Performed by</th><th>Date</th></tr>
    {action_rows}
  </table>
</section>

{decision_block}

<p class="footer">
  Decision-support tool only &mdash; no automated public health decisions are made.<br/>
  Northern Ireland TB Genomic Surveillance Programme &nbsp;|&nbsp; {h(generated_at)}
</p>
</body>
</html>"""

    return HTMLResponse(content=html_out)
