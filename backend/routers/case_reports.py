from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.auth import AuthenticatedUser, require_roles
from backend.data_safety import enforce_operational_dataset
from backend.routers import cases
from backend.routers import outbreak_html_builder
from backend.routers import outbreak_pdf_builder


router = APIRouter(prefix="/cases", tags=["cases"])


class ResistanceSignoffRequest(BaseModel):
    decision: str
    reviewer: str
    notes: str = ""
    catalogue_version: str = ""


@router.post("/resistance-validation/approve")
def resistance_validation_approve(
    payload: ResistanceSignoffRequest,
    db: Session = Depends(cases.get_db),
    _user: AuthenticatedUser = Depends(require_roles("operator")),
):
    """Record a formal sign-off for the local resistance pipeline validation."""
    allowed = {"approved", "rejected", "under_review"}
    if payload.decision not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"decision must be one of {sorted(allowed)}",
        )
    if not payload.reviewer.strip():
        raise HTTPException(status_code=422, detail="reviewer must not be empty")

    cases._ensure_signoff_table(db)
    row = db.execute(
        text(
            """
            INSERT INTO pipeline_validation_signoffs
                (pipeline, decision, reviewer, notes, catalogue_version)
            VALUES ('resistance_validation', :decision, :reviewer, :notes, :cat_ver)
            RETURNING id, pipeline, decision, reviewer, notes,
                      catalogue_version, signed_off_at
            """
        ),
        {
            "decision": payload.decision.strip(),
            "reviewer": payload.reviewer.strip(),
            "notes": (payload.notes or "").strip(),
            "cat_ver": (payload.catalogue_version or "").strip(),
        },
    ).mappings().first()
    db.commit()
    result = dict(row)
    if result.get("signed_off_at"):
        result["signed_off_at"] = str(result["signed_off_at"])
    return result


@router.get("/resistance-validation/status")
def resistance_validation_status(db: Session = Depends(cases.get_db)):
    """Return the current pipeline validation status (latest sign-off record)."""
    signoff = cases._get_latest_signoff(db)
    if signoff is None:
        return {"status": "under_review", "signoff": None}
    so = dict(signoff)
    if so.get("signed_off_at"):
        so["signed_off_at"] = str(so["signed_off_at"])
    return {"status": so["decision"], "signoff": so}


@router.get("/outbreak-report.html", response_class=HTMLResponse)
def outbreak_report_html(db: Session = Depends(cases.get_db)):
    """Generate and return the short publication-friendly static HTML outbreak report."""
    enforce_operational_dataset(db, "cases/outbreak-report.html")
    return HTMLResponse(content=outbreak_html_builder._build_outbreak_report_html(db, full=False))


@router.get("/outbreak-report.full.html", response_class=HTMLResponse)
def outbreak_report_full_html(db: Session = Depends(cases.get_db)):
    """Generate and return the full static HTML outbreak report alongside the short version."""
    enforce_operational_dataset(db, "cases/outbreak-report.full.html")
    return HTMLResponse(content=outbreak_html_builder._build_outbreak_report_html(db, full=True))


@router.get("/outbreak-report")
def outbreak_report(db: Session = Depends(cases.get_db)):
    """Generate and return a PDF outbreak investigation report."""
    return outbreak_pdf_builder.outbreak_report(db)
