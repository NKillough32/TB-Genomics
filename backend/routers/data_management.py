from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, StringConstraints, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.auth import AuthenticatedUser, require_roles
from backend.models import AuditLog, Case
from backend.routers.dependencies import get_db


router = APIRouter(prefix="/data-management", tags=["data-management"])

Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3)]

_CASE_FIELDS = (
    "local_lab_sample_id",
    "specimen_date",
    "geographic_region",
    "case_status",
    "symptom_onset_date",
    "treatment_start_date",
    "smear_status",
    "cavitation_status",
    "culture_status",
    "culture_positivity_duration_days",
    "infectiousness_notes",
)


class CaseUpdatePayload(BaseModel):
    reason: Reason
    local_lab_sample_id: str | None = None
    specimen_date: date | None = None
    geographic_region: str | None = None
    case_status: str | None = None
    symptom_onset_date: date | None = None
    treatment_start_date: date | None = None
    smear_status: str | None = None
    cavitation_status: str | None = None
    culture_status: str | None = None
    culture_positivity_duration_days: int | None = None
    infectiousness_notes: str | None = None

    @model_validator(mode="after")
    def _at_least_one_change(self):
        if not any(field in self.model_fields_set for field in _CASE_FIELDS):
            raise ValueError("At least one editable case field is required")
        return self


class CaseEnteredInErrorPayload(BaseModel):
    reason: Reason


class CaseRestorePayload(BaseModel):
    reason: Reason


def _write_audit(db: Session, action: str, user_id: str, details: dict) -> None:
    db.add(
        AuditLog(
            action=action,
            user_id=user_id,
            details=jsonable_encoder(details),
            timestamp=datetime.utcnow(),
        )
    )


def _clean_optional(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _case_snapshot(case: Case) -> dict:
    return {
        "case_id": str(case.pseudonymised_case_id),
        "case_short": str(case.pseudonymised_case_id)[:8],
        "local_lab_sample_id": case.local_lab_sample_id,
        "specimen_date": case.specimen_date,
        "geographic_region": case.geographic_region,
        "case_status": case.case_status,
        "symptom_onset_date": case.symptom_onset_date,
        "treatment_start_date": case.treatment_start_date,
        "smear_status": case.smear_status,
        "cavitation_status": case.cavitation_status,
        "culture_status": case.culture_status,
        "culture_positivity_duration_days": case.culture_positivity_duration_days,
        "infectiousness_notes": case.infectiousness_notes,
        "entered_in_error": bool(case.entered_in_error),
        "entered_in_error_at": case.entered_in_error_at,
        "entered_in_error_by": case.entered_in_error_by,
        "entered_in_error_reason": case.entered_in_error_reason,
        "created_at": case.created_at,
        "updated_at": case.updated_at,
    }


def _resolve_case(db: Session, case_id: str, include_entered_in_error: bool = True) -> Case:
    case_id = case_id.strip()
    try:
        parsed_id = UUID(case_id)
    except ValueError:
        parsed_id = None

    if parsed_id:
        case = db.get(Case, parsed_id)
    else:
        rows = db.execute(
            text(
                """
                SELECT pseudonymised_case_id
                FROM cases
                WHERE CAST(pseudonymised_case_id AS TEXT) LIKE :pattern
                ORDER BY created_at DESC NULLS LAST
                LIMIT 2
                """
            ),
            {"pattern": f"{case_id}%"},
        ).scalars().all()
        if len(rows) > 1:
            raise HTTPException(status_code=409, detail="Case prefix is ambiguous")
        case = db.get(Case, rows[0]) if rows else None

    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    if bool(case.entered_in_error) and not include_entered_in_error:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@router.get("/cases")
def list_managed_cases(
    query: str | None = None,
    include_entered_in_error: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query_sql = """
        SELECT
            c.pseudonymised_case_id::text AS case_id,
            c.local_lab_sample_id,
            c.specimen_date,
            c.geographic_region,
            c.case_status,
            c.entered_in_error,
            c.entered_in_error_at,
            c.entered_in_error_reason,
            c.updated_at,
            ti.lineage,
            sqm.qc_status,
            cc.cluster_id::text AS cluster_id
        FROM cases c
        LEFT JOIN tb_interpretation ti ON ti.sample_id = c.pseudonymised_case_id
        LEFT JOIN sample_qc_metrics sqm ON sqm.sample_id = c.pseudonymised_case_id
        LEFT JOIN case_clusters cc ON cc.sample_id = c.pseudonymised_case_id
        WHERE (:include_entered_in_error OR COALESCE(c.entered_in_error, false) = false)
    """
    params = {"include_entered_in_error": include_entered_in_error, "limit": limit}
    if query and query.strip():
        query_sql += """
          AND (
            CAST(c.pseudonymised_case_id AS TEXT) ILIKE :pattern
            OR c.local_lab_sample_id ILIKE :pattern
            OR c.geographic_region ILIKE :pattern
            OR c.case_status ILIKE :pattern
          )
        """
        params["pattern"] = f"%{query.strip()}%"
    query_sql += " ORDER BY c.updated_at DESC NULLS LAST, c.created_at DESC NULLS LAST LIMIT :limit"
    rows = db.execute(text(query_sql), params).mappings().all()
    return {"cases": [dict(row) for row in rows], "total_returned": len(rows)}


@router.get("/cases/{case_id}")
def get_managed_case(case_id: str, db: Session = Depends(get_db)):
    case = _resolve_case(db, case_id, include_entered_in_error=True)
    linked_counts = db.execute(
        text(
            """
            SELECT
                (SELECT COUNT(*) FROM tb_interpretation WHERE sample_id = :case_id)::int AS interpretation_rows,
                (SELECT COUNT(*) FROM consensus_sequences WHERE sample_id = :case_id)::int AS sequence_rows,
                (SELECT COUNT(*) FROM sample_qc_metrics WHERE sample_id = :case_id)::int AS qc_rows,
                (SELECT COUNT(*) FROM case_clusters WHERE sample_id = :case_id)::int AS cluster_links,
                (SELECT COUNT(*) FROM resistance_calls WHERE sample_id = :case_id)::int AS resistance_calls,
                (SELECT COUNT(*) FROM alerts WHERE sample_id = :case_id)::int AS alerts,
                (SELECT COUNT(*) FROM actions WHERE sample_id = :case_id)::int AS actions
            """
        ),
        {"case_id": case.pseudonymised_case_id},
    ).mappings().first()
    return {"case": _case_snapshot(case), "linked_counts": dict(linked_counts or {})}


@router.patch("/cases/{case_id}")
def update_managed_case(
    case_id: str,
    payload: CaseUpdatePayload,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("admin")),
):
    case = _resolve_case(db, case_id, include_entered_in_error=True)
    before = _case_snapshot(case)
    updates = payload.model_dump(exclude_unset=True)
    updates.pop("reason", None)
    for field, value in updates.items():
        setattr(case, field, _clean_optional(value))
    case.updated_at = datetime.utcnow()
    after = _case_snapshot(case)
    changed = {
        field: {"from": before.get(field), "to": after.get(field)}
        for field in _CASE_FIELDS
        if before.get(field) != after.get(field)
    }
    if not changed:
        raise HTTPException(status_code=422, detail="No case values changed")
    _write_audit(
        db,
        "case_updated",
        user.subject,
        {"case_id": str(case.pseudonymised_case_id), "reason": payload.reason, "changed": changed},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Case update violates a unique constraint") from exc
    db.refresh(case)
    return {"status": "updated", "case": _case_snapshot(case), "changed": changed}


@router.delete("/cases/{case_id}")
def mark_case_entered_in_error(
    case_id: str,
    payload: CaseEnteredInErrorPayload,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("admin")),
):
    case = _resolve_case(db, case_id, include_entered_in_error=True)
    if bool(case.entered_in_error):
        return {"status": "already_entered_in_error", "case": _case_snapshot(case)}
    case.entered_in_error = True
    case.entered_in_error_at = datetime.utcnow()
    case.entered_in_error_by = user.subject
    case.entered_in_error_reason = payload.reason
    case.updated_at = datetime.utcnow()
    _write_audit(
        db,
        "case_entered_in_error",
        user.subject,
        {"case_id": str(case.pseudonymised_case_id), "reason": payload.reason},
    )
    db.commit()
    db.refresh(case)
    return {"status": "entered_in_error", "case": _case_snapshot(case)}


@router.post("/cases/{case_id}/restore")
def restore_managed_case(
    case_id: str,
    payload: CaseRestorePayload,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("admin")),
):
    case = _resolve_case(db, case_id, include_entered_in_error=True)
    if not bool(case.entered_in_error):
        return {"status": "already_active", "case": _case_snapshot(case)}
    previous_reason = case.entered_in_error_reason
    case.entered_in_error = False
    case.entered_in_error_at = None
    case.entered_in_error_by = None
    case.entered_in_error_reason = None
    case.updated_at = datetime.utcnow()
    _write_audit(
        db,
        "case_restored",
        user.subject,
        {
            "case_id": str(case.pseudonymised_case_id),
            "reason": payload.reason,
            "previous_entered_in_error_reason": previous_reason,
        },
    )
    db.commit()
    db.refresh(case)
    return {"status": "restored", "case": _case_snapshot(case)}
