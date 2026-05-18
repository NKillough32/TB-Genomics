"""Structured epidemiological evidence for case pair transmission synthesis.

For each case pair this module queries the relational epi records already
stored in the database (contacts, locations, exposures, case-contact links,
case-location events) and returns a structured evidence dict.

The returned dict replaces the legacy proxy approach (specimen-date proximity
+ same geographic region) that was previously used in transmission_synthesis.py.

Usage
-----
Bulk-load records once per synthesis run, then compute per-pair cheaply::

    from backend.synthesis.epi_evidence import load_epi_records_for_cases, compute_epi_evidence

    records = load_epi_records_for_cases(db, case_ids=list(case_index.keys()))
    evidence = compute_epi_evidence("case-a-uuid", "case-b-uuid", records,
                                    specimen_date_a=..., specimen_date_b=...,
                                    temporal_window_days=cfg.temporal_window_days)
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONF_WEIGHT: dict[str | None, float] = {
    "high": 1.0,
    "medium": 0.6,
    "low": 0.3,
    None: 0.4,
}


def _conf_weight(value: str | None) -> float:
    return _CONF_WEIGHT.get((value or "").strip().lower() or None, 0.4)


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.fromisoformat(str(value)).date()
    except Exception:
        return None


def _overlap_days(
    start_a: date | None,
    end_a: date | None,
    start_b: date | None,
    end_b: date | None,
) -> int | None:
    """
    Return the number of overlapping days between two date intervals.

    Uses half-open intervals [start, end).  If both ends are open (None) the
    intervals are treated as ongoing up to today.  Returns None when any
    required bound is missing.
    """
    today = date.today()
    sa = start_a
    sb = start_b
    if sa is None or sb is None:
        return None
    ea = end_a or today
    eb = end_b or today
    latest_start = max(sa, sb)
    earliest_end = min(ea, eb)
    days = (earliest_end - latest_start).days
    return max(0, days)


# ---------------------------------------------------------------------------
# Bulk loader
# ---------------------------------------------------------------------------

def load_epi_records_for_cases(
    db: Session,
    *,
    case_ids: list[str],
) -> dict[str, Any]:
    """
    Load all epi records for the given case IDs in a small number of queries.

    Returns a dict with the following structure::

        {
            "contact_links": {
                "<case_id>": [
                    {
                        "link_id": str,
                        "contact_id": str,
                        "contact_label": str,
                        "contact_type": str | None,
                        "relationship_type": str | None,
                        "exposure_id": str | None,
                        "exposure_type": str | None,
                        "confidence": str | None,
                        "exposure_start_date": date | None,
                        "exposure_end_date": date | None,
                    },
                    ...
                ]
            },
            "location_events": {
                "<case_id>": [
                    {
                        "event_id": str,
                        "location_id": str,
                        "location_name": str,
                        "location_type": str | None,
                        "event_type": str,
                        "arrived_at": date | None,
                        "departed_at": date | None,
                        "exposure_id": str | None,
                        "exposure_type": str | None,
                        "confidence": str | None,
                    },
                    ...
                ]
            },
        }
    """
    if not case_ids:
        return {"contact_links": {}, "location_events": {}}

    # Build parameterised IN clause safely
    placeholders = ", ".join(f":cid_{i}" for i in range(len(case_ids)))
    params: dict[str, Any] = {f"cid_{i}": cid for i, cid in enumerate(case_ids)}

    # --- contact links -------------------------------------------------------
    contact_sql = f"""
        SELECT
            ccl.link_id::text,
            ccl.case_id::text,
            ccl.contact_id::text,
            ccl.exposure_id::text,
            ccl.link_type,
            ccl.exposure_start_date,
            ccl.exposure_end_date,
            ccl.confidence,
            c.contact_label,
            c.contact_type,
            c.relationship_type,
            e.exposure_type
        FROM case_contact_links ccl
        JOIN contacts c ON c.contact_id = ccl.contact_id
        LEFT JOIN exposures e ON e.exposure_id = ccl.exposure_id
        WHERE ccl.case_id IN ({placeholders})
    """

    contact_rows = db.execute(text(contact_sql), params).mappings().all()

    contact_links: dict[str, list[dict[str, Any]]] = {}
    for row in contact_rows:
        cid = str(row["case_id"])
        contact_links.setdefault(cid, []).append(
            {
                "link_id": str(row["link_id"]),
                "contact_id": str(row["contact_id"]),
                "contact_label": str(row["contact_label"] or ""),
                "contact_type": row["contact_type"],
                "relationship_type": row["relationship_type"],
                "exposure_id": str(row["exposure_id"]) if row["exposure_id"] else None,
                "exposure_type": row["exposure_type"],
                "confidence": row["confidence"],
                "exposure_start_date": _to_date(row["exposure_start_date"]),
                "exposure_end_date": _to_date(row["exposure_end_date"]),
            }
        )

    # --- location events -----------------------------------------------------
    location_sql = f"""
        SELECT
            cle.event_id::text,
            cle.case_id::text,
            cle.location_id::text,
            cle.exposure_id::text,
            cle.event_type,
            cle.arrived_at,
            cle.departed_at,
            cle.confidence,
            l.location_name,
            l.location_type,
            l.geographic_region,
            e.exposure_type
        FROM case_location_events cle
        JOIN locations l ON l.location_id = cle.location_id
        LEFT JOIN exposures e ON e.exposure_id = cle.exposure_id
        WHERE cle.case_id IN ({placeholders})
    """

    location_rows = db.execute(text(location_sql), params).mappings().all()

    location_events: dict[str, list[dict[str, Any]]] = {}
    for row in location_rows:
        cid = str(row["case_id"])
        location_events.setdefault(cid, []).append(
            {
                "event_id": str(row["event_id"]),
                "location_id": str(row["location_id"]),
                "location_name": str(row["location_name"] or ""),
                "location_type": row["location_type"],
                "geographic_region": row["geographic_region"],
                "event_type": str(row["event_type"] or ""),
                "arrived_at": _to_date(row["arrived_at"]),
                "departed_at": _to_date(row["departed_at"]),
                "exposure_id": str(row["exposure_id"]) if row["exposure_id"] else None,
                "exposure_type": row["exposure_type"],
                "confidence": row["confidence"],
            }
        )

    return {"contact_links": contact_links, "location_events": location_events}


# ---------------------------------------------------------------------------
# Per-pair evidence computation
# ---------------------------------------------------------------------------

def compute_epi_evidence(
    case_id_a: str,
    case_id_b: str,
    records: dict[str, Any],
    *,
    specimen_date_a: date | None = None,
    specimen_date_b: None | date = None,
    temporal_window_days: int = 45,
) -> dict[str, Any]:
    """
    Compute structured epi evidence for a single case pair from pre-loaded records.

    Parameters
    ----------
    case_id_a, case_id_b:
        The UUIDs of the two cases (as str).
    records:
        Dict returned by :func:`load_epi_records_for_cases`.
    specimen_date_a, specimen_date_b:
        Specimen dates used as a fallback for temporal plausibility when no
        infectious-period dates are available.
    temporal_window_days:
        Days within which two specimen dates are considered temporally plausible.

    Returns
    -------
    dict with keys:
        shared_locations, shared_contacts, shared_exposures,
        temporal_overlap, epi_support_level, contradictions, missing_data
    """
    links_a = records.get("contact_links", {}).get(case_id_a, [])
    links_b = records.get("contact_links", {}).get(case_id_b, [])
    events_a = records.get("location_events", {}).get(case_id_a, [])
    events_b = records.get("location_events", {}).get(case_id_b, [])

    # ------------------------------------------------------------------
    # Shared contacts
    # ------------------------------------------------------------------
    contact_ids_a = {lk["contact_id"]: lk for lk in links_a}
    contact_ids_b = {lk["contact_id"]: lk for lk in links_b}
    shared_contact_ids = set(contact_ids_a) & set(contact_ids_b)

    shared_contacts: list[dict[str, Any]] = []
    for cid in shared_contact_ids:
        lka = contact_ids_a[cid]
        lkb = contact_ids_b[cid]
        min_conf = min(_conf_weight(lka["confidence"]), _conf_weight(lkb["confidence"]))
        shared_contacts.append(
            {
                "contact_id": cid,
                "contact_label": lka["contact_label"],
                "contact_type": lka["contact_type"],
                "confidence_a": lka["confidence"],
                "confidence_b": lkb["confidence"],
                "evidence_weight": round(min_conf, 3),
            }
        )

    # ------------------------------------------------------------------
    # Shared locations
    # ------------------------------------------------------------------
    loc_ids_a: dict[str, list[dict[str, Any]]] = {}
    for ev in events_a:
        loc_ids_a.setdefault(ev["location_id"], []).append(ev)

    loc_ids_b: dict[str, list[dict[str, Any]]] = {}
    for ev in events_b:
        loc_ids_b.setdefault(ev["location_id"], []).append(ev)

    shared_location_ids = set(loc_ids_a) & set(loc_ids_b)

    shared_locations: list[dict[str, Any]] = []
    for lid in shared_location_ids:
        evs_a = loc_ids_a[lid]
        evs_b = loc_ids_b[lid]
        # Find the best-overlapping attendance window pair
        best_overlap: int | None = None
        best_conf_weight = 0.0
        for ea in evs_a:
            for eb in evs_b:
                ov = _overlap_days(ea["arrived_at"], ea["departed_at"],
                                   eb["arrived_at"], eb["departed_at"])
                cw = min(_conf_weight(ea["confidence"]), _conf_weight(eb["confidence"]))
                if best_overlap is None or (ov is not None and ov > best_overlap):
                    best_overlap = ov
                if cw > best_conf_weight:
                    best_conf_weight = cw

        sample_ev = evs_a[0]
        shared_locations.append(
            {
                "location_id": lid,
                "location_name": sample_ev["location_name"],
                "location_type": sample_ev["location_type"],
                "attendance_overlap_days": best_overlap,
                "evidence_weight": round(best_conf_weight, 3),
            }
        )

    # ------------------------------------------------------------------
    # Shared exposures (same exposure_id on either links or events)
    # ------------------------------------------------------------------
    exp_ids_a = {lk["exposure_id"] for lk in links_a if lk["exposure_id"]}
    exp_ids_a |= {ev["exposure_id"] for ev in events_a if ev["exposure_id"]}
    exp_ids_b = {lk["exposure_id"] for lk in links_b if lk["exposure_id"]}
    exp_ids_b |= {ev["exposure_id"] for ev in events_b if ev["exposure_id"]}
    shared_exp_ids = exp_ids_a & exp_ids_b

    # Build an exposure_id -> type lookup from available records
    exp_type_lookup: dict[str, str] = {}
    for lk in links_a + links_b:
        if lk["exposure_id"] and lk["exposure_type"]:
            exp_type_lookup[lk["exposure_id"]] = lk["exposure_type"]
    for ev in events_a + events_b:
        if ev["exposure_id"] and ev["exposure_type"]:
            exp_type_lookup[ev["exposure_id"]] = ev["exposure_type"]

    shared_exposures: list[dict[str, Any]] = [
        {"exposure_id": eid, "exposure_type": exp_type_lookup.get(eid)}
        for eid in shared_exp_ids
    ]

    # ------------------------------------------------------------------
    # Temporal overlap (specimen date proximity as fallback)
    # ------------------------------------------------------------------
    temporal_overlap = "unknown"
    if specimen_date_a and specimen_date_b:
        delta = abs((specimen_date_b - specimen_date_a).days)
        if delta <= temporal_window_days:
            temporal_overlap = "plausible"
        elif delta <= temporal_window_days * 2:
            temporal_overlap = "marginal"
        else:
            temporal_overlap = "implausible"

    # ------------------------------------------------------------------
    # Missing data flags
    # ------------------------------------------------------------------
    missing_data: list[str] = []
    if not links_a and not events_a:
        missing_data.append("no_epi_records_case_a")
    if not links_b and not events_b:
        missing_data.append("no_epi_records_case_b")
    if not specimen_date_a:
        missing_data.append("missing_specimen_date_case_a")
    if not specimen_date_b:
        missing_data.append("missing_specimen_date_case_b")

    # ------------------------------------------------------------------
    # Contradictions
    # ------------------------------------------------------------------
    contradictions: list[str] = []
    if temporal_overlap == "implausible" and (shared_contacts or shared_locations):
        contradictions.append(
            "shared_epi_link_but_implausible_temporal_overlap"
        )

    # ------------------------------------------------------------------
    # epi_support_level
    # ------------------------------------------------------------------
    has_strong_contact = any(s["evidence_weight"] >= 0.6 for s in shared_contacts)
    has_strong_location = any(
        s["evidence_weight"] >= 0.6
        and (s["attendance_overlap_days"] is None or s["attendance_overlap_days"] >= 0)
        for s in shared_locations
    )
    has_any_shared = bool(shared_contacts or shared_locations or shared_exposures)
    has_plausible_time = temporal_overlap in {"plausible", "marginal"}

    if (has_strong_contact or has_strong_location) and has_plausible_time:
        epi_support_level = "strong"
    elif has_any_shared and has_plausible_time:
        epi_support_level = "moderate"
    elif has_any_shared:
        epi_support_level = "weak"
    elif temporal_overlap == "plausible":
        epi_support_level = "temporal_only"
    elif temporal_overlap == "implausible":
        epi_support_level = "none"
    else:
        epi_support_level = "unknown"

    return {
        "shared_locations": shared_locations,
        "shared_contacts": shared_contacts,
        "shared_exposures": shared_exposures,
        "temporal_overlap": temporal_overlap,
        "epi_support_level": epi_support_level,
        "contradictions": contradictions,
        "missing_data": missing_data,
    }
