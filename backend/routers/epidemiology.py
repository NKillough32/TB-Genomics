from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.orm import Session
from typing import Annotated

from backend.auth import AuthenticatedUser, require_roles
from backend.models import AuditLog, CaseContactLink, CaseLocationEvent, Contact, Exposure, Location
from backend.routers.case_overview import get_db


router = APIRouter(prefix="/epidemiology", tags=["epidemiology"])

Confidence = Annotated[str, StringConstraints(strip_whitespace=True, pattern="^(low|medium|high)$")]


def _write_audit(db: Session, action: str, user_id: str, details: dict) -> None:
    """Append an audit log row within the current transaction (caller must commit)."""
    db.add(AuditLog(action=action, user_id=user_id, details=details, timestamp=datetime.utcnow()))


class ExposureCreate(BaseModel):
    exposure_type: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    exposure_context: str | None = None
    exposure_start_date: date | None = None
    exposure_end_date: date | None = None
    confidence: Confidence | None = None
    source: str | None = None
    notes: str | None = None


class ExposureOut(ExposureCreate):
    model_config = ConfigDict(from_attributes=True)

    exposure_id: UUID
    entered_in_error: bool = False
    entered_in_error_at: datetime | None = None
    entered_in_error_by: str | None = None
    entered_in_error_reason: str | None = None
    created_at: datetime | None = None


class ContactCreate(BaseModel):
    contact_label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    contact_type: str | None = None
    relationship_type: str | None = None
    pseudonymised_identifier: str | None = None


class ContactOut(ContactCreate):
    model_config = ConfigDict(from_attributes=True)

    contact_id: UUID
    entered_in_error: bool = False
    entered_in_error_at: datetime | None = None
    entered_in_error_by: str | None = None
    entered_in_error_reason: str | None = None
    created_at: datetime | None = None


class LocationCreate(BaseModel):
    location_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    location_type: str | None = None
    address_line: str | None = None
    geographic_region: str | None = None
    postcode_prefix: str | None = None


class LocationOut(LocationCreate):
    model_config = ConfigDict(from_attributes=True)

    location_id: UUID
    entered_in_error: bool = False
    entered_in_error_at: datetime | None = None
    entered_in_error_by: str | None = None
    entered_in_error_reason: str | None = None
    created_at: datetime | None = None


class CaseLocationEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: UUID
    case_id: UUID
    location_id: UUID
    exposure_id: UUID | None = None
    event_type: str
    arrived_at: datetime | None = None
    departed_at: datetime | None = None
    confidence: str | None = None
    source: str | None = None
    notes: str | None = None
    entered_in_error: bool = False
    entered_in_error_at: datetime | None = None
    entered_in_error_by: str | None = None
    entered_in_error_reason: str | None = None
    created_at: datetime | None = None


class CaseLocationEventCreate(BaseModel):
    case_id: UUID
    event_type: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    location_id: UUID | None = None
    location_name: str | None = None
    location_type: str | None = None
    address_line: str | None = None
    geographic_region: str | None = None
    postcode_prefix: str | None = None
    exposure_id: UUID | None = None
    exposure_type: str | None = None
    exposure_context: str | None = None
    arrived_at: datetime | None = None
    departed_at: datetime | None = None
    confidence: Confidence | None = None
    source: str | None = None
    notes: str | None = None


class CaseLocationEventUpdate(BaseModel):
    event_type: str | None = None
    location_id: UUID | None = None
    exposure_id: UUID | None = None
    arrived_at: datetime | None = None
    departed_at: datetime | None = None
    confidence: Confidence | None = None
    source: str | None = None
    notes: str | None = None


class CaseContactLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    link_id: UUID
    case_id: UUID
    contact_id: UUID
    exposure_id: UUID | None = None
    link_type: str | None = None
    exposure_start_date: date | None = None
    exposure_end_date: date | None = None
    source: str | None = None
    confidence: str | None = None
    notes: str | None = None
    entered_in_error: bool = False
    entered_in_error_at: datetime | None = None
    entered_in_error_by: str | None = None
    entered_in_error_reason: str | None = None
    created_at: datetime | None = None


class EnteredInErrorPayload(BaseModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CaseContactLinkCreate(BaseModel):
    case_id: UUID
    contact_id: UUID | None = None
    contact_label: str | None = None
    contact_type: str | None = None
    relationship_type: str | None = None
    pseudonymised_identifier: str | None = None
    exposure_id: UUID | None = None
    exposure_type: str | None = None
    exposure_context: str | None = None
    link_type: str | None = None
    exposure_start_date: date | None = None
    exposure_end_date: date | None = None
    source: str | None = None
    confidence: Confidence | None = None
    notes: str | None = None


class CaseContactLinkUpdate(BaseModel):
    contact_id: UUID | None = None
    exposure_id: UUID | None = None
    link_type: str | None = None
    exposure_start_date: date | None = None
    exposure_end_date: date | None = None
    source: str | None = None
    confidence: Confidence | None = None
    notes: str | None = None


def _get_or_404(db: Session, model, primary_key: UUID, label: str):
    item = db.get(model, primary_key)
    if item is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return item


def _get_active_or_404(db: Session, model, primary_key: UUID, label: str):
    item = _get_or_404(db, model, primary_key, label)
    if bool(getattr(item, "entered_in_error", False)):
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return item


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _create_inline_exposure(
    db: Session,
    exposure_id: UUID | None,
    exposure_type: str | None,
    exposure_context: str | None,
    confidence: str | None,
    source: str | None,
    notes: str | None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> UUID | None:
    if exposure_id:
        _get_active_or_404(db, Exposure, exposure_id, "Exposure")
        return exposure_id
    exposure_type = _clean_optional(exposure_type)
    if not exposure_type:
        return None
    exposure = Exposure(
        exposure_type=exposure_type,
        exposure_context=_clean_optional(exposure_context),
        exposure_start_date=start_date,
        exposure_end_date=end_date,
        confidence=confidence,
        source=_clean_optional(source),
        notes=_clean_optional(notes),
    )
    db.add(exposure)
    db.flush()
    return exposure.exposure_id


@router.get("/exposures", response_model=list[ExposureOut])
def list_exposures(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    include_entered_in_error: bool = Query(False),
    db: Session = Depends(get_db),
):
    query = db.query(Exposure)
    if not include_entered_in_error:
        query = query.filter(Exposure.entered_in_error.is_(False))
    return query.order_by(Exposure.created_at.desc()).offset(offset).limit(limit).all()


@router.post("/exposures", response_model=ExposureOut)
def create_exposure(
    payload: ExposureCreate,
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_roles("operator")),
):
    exposure = Exposure(**payload.model_dump())
    db.add(exposure)
    db.flush()
    _write_audit(db, "exposure_created", _user.subject,
                 {"exposure_id": str(exposure.exposure_id), "exposure_type": exposure.exposure_type})
    db.commit()
    db.refresh(exposure)
    return exposure


@router.get("/exposures/{exposure_id}", response_model=ExposureOut)
def get_exposure(exposure_id: UUID, db: Session = Depends(get_db)):
    return _get_active_or_404(db, Exposure, exposure_id, "Exposure")


@router.get("/contacts", response_model=list[ContactOut])
def list_contacts(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    include_entered_in_error: bool = Query(False),
    db: Session = Depends(get_db),
):
    query = db.query(Contact)
    if not include_entered_in_error:
        query = query.filter(Contact.entered_in_error.is_(False))
    return query.order_by(Contact.created_at.desc()).offset(offset).limit(limit).all()


@router.post("/contacts", response_model=ContactOut)
def create_contact(
    payload: ContactCreate,
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_roles("operator")),
):
    contact = Contact(**payload.model_dump())
    db.add(contact)
    db.flush()
    _write_audit(db, "contact_created", _user.subject,
                 {"contact_id": str(contact.contact_id), "contact_label": contact.contact_label})
    db.commit()
    db.refresh(contact)
    return contact


@router.get("/contacts/{contact_id}", response_model=ContactOut)
def get_contact(contact_id: UUID, db: Session = Depends(get_db)):
    return _get_active_or_404(db, Contact, contact_id, "Contact")


@router.get("/locations", response_model=list[LocationOut])
def list_locations(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    include_entered_in_error: bool = Query(False),
    db: Session = Depends(get_db),
):
    query = db.query(Location)
    if not include_entered_in_error:
        query = query.filter(Location.entered_in_error.is_(False))
    return query.order_by(Location.created_at.desc()).offset(offset).limit(limit).all()


@router.post("/locations", response_model=LocationOut)
def create_location(
    payload: LocationCreate,
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_roles("operator")),
):
    location = Location(**payload.model_dump())
    db.add(location)
    db.flush()
    _write_audit(db, "location_created", _user.subject,
                 {"location_id": str(location.location_id), "location_name": location.location_name})
    db.commit()
    db.refresh(location)
    return location


@router.get("/locations/{location_id}", response_model=LocationOut)
def get_location(location_id: UUID, db: Session = Depends(get_db)):
    return _get_active_or_404(db, Location, location_id, "Location")


@router.get("/case-location-events", response_model=list[CaseLocationEventOut])
def list_case_location_events(
    case_id: UUID | None = None,
    location_id: UUID | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    include_entered_in_error: bool = Query(False),
    db: Session = Depends(get_db),
):
    query = db.query(CaseLocationEvent)
    if not include_entered_in_error:
        query = query.filter(CaseLocationEvent.entered_in_error.is_(False))
    if case_id:
        query = query.filter(CaseLocationEvent.case_id == case_id)
    if location_id:
        query = query.filter(CaseLocationEvent.location_id == location_id)
    return query.order_by(CaseLocationEvent.created_at.desc()).offset(offset).limit(limit).all()


@router.post("/case-location-events", response_model=CaseLocationEventOut)
def create_case_location_event(
    payload: CaseLocationEventCreate,
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_roles("operator")),
):
    location_id = payload.location_id
    if location_id:
        _get_active_or_404(db, Location, location_id, "Location")
    else:
        location_name = _clean_optional(payload.location_name)
        if not location_name:
            raise HTTPException(
                status_code=422,
                detail="location_id or location_name is required",
            )
        location = Location(
            location_name=location_name,
            location_type=_clean_optional(payload.location_type),
            address_line=_clean_optional(payload.address_line),
            geographic_region=_clean_optional(payload.geographic_region),
            postcode_prefix=_clean_optional(payload.postcode_prefix),
        )
        db.add(location)
        db.flush()
        location_id = location.location_id

    exposure_id = _create_inline_exposure(
        db,
        payload.exposure_id,
        payload.exposure_type,
        payload.exposure_context,
        payload.confidence,
        payload.source,
        payload.notes,
    )
    event = CaseLocationEvent(
        case_id=payload.case_id,
        location_id=location_id,
        exposure_id=exposure_id,
        event_type=payload.event_type.strip(),
        arrived_at=payload.arrived_at,
        departed_at=payload.departed_at,
        confidence=payload.confidence,
        source=_clean_optional(payload.source),
        notes=_clean_optional(payload.notes),
    )
    db.add(event)
    db.flush()
    _write_audit(db, "case_location_event_created", _user.subject,
                 {"event_id": str(event.event_id), "case_id": str(event.case_id),
                  "location_id": str(event.location_id), "event_type": event.event_type})
    db.commit()
    db.refresh(event)
    return event


@router.put("/case-location-events/{event_id}", response_model=CaseLocationEventOut)
def update_case_location_event(
    event_id: UUID,
    payload: CaseLocationEventUpdate,
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_roles("operator")),
):
    event = _get_active_or_404(db, CaseLocationEvent, event_id, "Case location event")
    updates = payload.model_dump(exclude_unset=True)
    if "location_id" in updates and updates["location_id"] is not None:
        _get_active_or_404(db, Location, updates["location_id"], "Location")
    if "exposure_id" in updates and updates["exposure_id"] is not None:
        _get_active_or_404(db, Exposure, updates["exposure_id"], "Exposure")
    if updates.get("event_type") is not None:
        updates["event_type"] = updates["event_type"].strip()
        if not updates["event_type"]:
            raise HTTPException(status_code=422, detail="event_type must not be empty")
    for key, value in updates.items():
        setattr(event, key, value)
    _write_audit(db, "case_location_event_updated", _user.subject,
                 {"event_id": str(event_id), "updated_fields": list(updates.keys())})
    db.commit()
    db.refresh(event)
    return event


@router.get("/case-contact-links", response_model=list[CaseContactLinkOut])
def list_case_contact_links(
    case_id: UUID | None = None,
    contact_id: UUID | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    include_entered_in_error: bool = Query(False),
    db: Session = Depends(get_db),
):
    query = db.query(CaseContactLink)
    if not include_entered_in_error:
        query = query.filter(CaseContactLink.entered_in_error.is_(False))
    if case_id:
        query = query.filter(CaseContactLink.case_id == case_id)
    if contact_id:
        query = query.filter(CaseContactLink.contact_id == contact_id)
    return query.order_by(CaseContactLink.created_at.desc()).offset(offset).limit(limit).all()


@router.post("/case-contact-links", response_model=CaseContactLinkOut)
def create_case_contact_link(
    payload: CaseContactLinkCreate,
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_roles("operator")),
):
    contact_id = payload.contact_id
    if contact_id:
        _get_active_or_404(db, Contact, contact_id, "Contact")
    else:
        contact_label = _clean_optional(payload.contact_label)
        if not contact_label:
            raise HTTPException(
                status_code=422,
                detail="contact_id or contact_label is required",
            )
        contact = Contact(
            contact_label=contact_label,
            contact_type=_clean_optional(payload.contact_type),
            relationship_type=_clean_optional(payload.relationship_type),
            pseudonymised_identifier=_clean_optional(payload.pseudonymised_identifier),
        )
        db.add(contact)
        db.flush()
        contact_id = contact.contact_id

    exposure_id = _create_inline_exposure(
        db,
        payload.exposure_id,
        payload.exposure_type,
        payload.exposure_context,
        payload.confidence,
        payload.source,
        payload.notes,
        payload.exposure_start_date,
        payload.exposure_end_date,
    )
    link = CaseContactLink(
        case_id=payload.case_id,
        contact_id=contact_id,
        exposure_id=exposure_id,
        link_type=_clean_optional(payload.link_type),
        exposure_start_date=payload.exposure_start_date,
        exposure_end_date=payload.exposure_end_date,
        source=_clean_optional(payload.source),
        confidence=payload.confidence,
        notes=_clean_optional(payload.notes),
    )
    db.add(link)
    db.flush()
    _write_audit(db, "case_contact_link_created", _user.subject,
                 {"link_id": str(link.link_id), "case_id": str(link.case_id),
                  "contact_id": str(link.contact_id)})
    db.commit()
    db.refresh(link)
    return link


@router.put("/case-contact-links/{link_id}", response_model=CaseContactLinkOut)
def update_case_contact_link(
    link_id: UUID,
    payload: CaseContactLinkUpdate,
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_roles("operator")),
):
    link = _get_active_or_404(db, CaseContactLink, link_id, "Case contact link")
    updates = payload.model_dump(exclude_unset=True)
    if "contact_id" in updates and updates["contact_id"] is not None:
        _get_active_or_404(db, Contact, updates["contact_id"], "Contact")
    if "exposure_id" in updates and updates["exposure_id"] is not None:
        _get_active_or_404(db, Exposure, updates["exposure_id"], "Exposure")
    for key, value in updates.items():
        setattr(link, key, value)
    _write_audit(db, "case_contact_link_updated", _user.subject,
                 {"link_id": str(link_id), "updated_fields": list(updates.keys())})
    db.commit()
    db.refresh(link)
    return link


def _mark_entered_in_error(item, user: AuthenticatedUser, reason: str) -> None:
    item.entered_in_error = True
    item.entered_in_error_at = datetime.utcnow()
    item.entered_in_error_by = user.subject
    item.entered_in_error_reason = reason.strip()


def _restore_from_entered_in_error(item) -> None:
    item.entered_in_error = False
    item.entered_in_error_at = None
    item.entered_in_error_by = None
    item.entered_in_error_reason = None


@router.post("/exposures/{exposure_id}/entered-in-error", response_model=ExposureOut)
def mark_exposure_entered_in_error(
    exposure_id: UUID,
    payload: EnteredInErrorPayload,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("operator")),
):
    exposure = _get_or_404(db, Exposure, exposure_id, "Exposure")
    _mark_entered_in_error(exposure, user, payload.reason)
    _write_audit(db, "exposure_entered_in_error", user.subject,
                 {"exposure_id": str(exposure_id), "reason": payload.reason.strip()})
    db.commit()
    db.refresh(exposure)
    return exposure


@router.post("/contacts/{contact_id}/entered-in-error", response_model=ContactOut)
def mark_contact_entered_in_error(
    contact_id: UUID,
    payload: EnteredInErrorPayload,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("operator")),
):
    contact = _get_or_404(db, Contact, contact_id, "Contact")
    _mark_entered_in_error(contact, user, payload.reason)
    _write_audit(db, "contact_entered_in_error", user.subject,
                 {"contact_id": str(contact_id), "reason": payload.reason.strip()})
    db.commit()
    db.refresh(contact)
    return contact


@router.post("/locations/{location_id}/entered-in-error", response_model=LocationOut)
def mark_location_entered_in_error(
    location_id: UUID,
    payload: EnteredInErrorPayload,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("operator")),
):
    location = _get_or_404(db, Location, location_id, "Location")
    _mark_entered_in_error(location, user, payload.reason)
    _write_audit(db, "location_entered_in_error", user.subject,
                 {"location_id": str(location_id), "reason": payload.reason.strip()})
    db.commit()
    db.refresh(location)
    return location


@router.post("/case-location-events/{event_id}/entered-in-error", response_model=CaseLocationEventOut)
def mark_case_location_event_entered_in_error(
    event_id: UUID,
    payload: EnteredInErrorPayload,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("operator")),
):
    event = _get_or_404(db, CaseLocationEvent, event_id, "Case location event")
    _mark_entered_in_error(event, user, payload.reason)
    _write_audit(db, "case_location_event_entered_in_error", user.subject,
                 {"event_id": str(event_id), "reason": payload.reason.strip()})
    db.commit()
    db.refresh(event)
    return event


@router.post("/case-contact-links/{link_id}/entered-in-error", response_model=CaseContactLinkOut)
def mark_case_contact_link_entered_in_error(
    link_id: UUID,
    payload: EnteredInErrorPayload,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("operator")),
):
    link = _get_or_404(db, CaseContactLink, link_id, "Case contact link")
    _mark_entered_in_error(link, user, payload.reason)
    _write_audit(db, "case_contact_link_entered_in_error", user.subject,
                 {"link_id": str(link_id), "reason": payload.reason.strip()})
    db.commit()
    db.refresh(link)
    return link


@router.post("/exposures/{exposure_id}/restore", response_model=ExposureOut)
def restore_exposure(
    exposure_id: UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("operator")),
):
    exposure = _get_or_404(db, Exposure, exposure_id, "Exposure")
    _restore_from_entered_in_error(exposure)
    _write_audit(db, "exposure_restored", user.subject, {"exposure_id": str(exposure_id)})
    db.commit()
    db.refresh(exposure)
    return exposure


@router.post("/contacts/{contact_id}/restore", response_model=ContactOut)
def restore_contact(
    contact_id: UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("operator")),
):
    contact = _get_or_404(db, Contact, contact_id, "Contact")
    _restore_from_entered_in_error(contact)
    _write_audit(db, "contact_restored", user.subject, {"contact_id": str(contact_id)})
    db.commit()
    db.refresh(contact)
    return contact


@router.post("/locations/{location_id}/restore", response_model=LocationOut)
def restore_location(
    location_id: UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("operator")),
):
    location = _get_or_404(db, Location, location_id, "Location")
    _restore_from_entered_in_error(location)
    _write_audit(db, "location_restored", user.subject, {"location_id": str(location_id)})
    db.commit()
    db.refresh(location)
    return location


@router.post("/case-location-events/{event_id}/restore", response_model=CaseLocationEventOut)
def restore_case_location_event(
    event_id: UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("operator")),
):
    event = _get_or_404(db, CaseLocationEvent, event_id, "Case location event")
    _restore_from_entered_in_error(event)
    _write_audit(db, "case_location_event_restored", user.subject, {"event_id": str(event_id)})
    db.commit()
    db.refresh(event)
    return event


@router.post("/case-contact-links/{link_id}/restore", response_model=CaseContactLinkOut)
def restore_case_contact_link(
    link_id: UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles("operator")),
):
    link = _get_or_404(db, CaseContactLink, link_id, "Case contact link")
    _restore_from_entered_in_error(link)
    _write_audit(db, "case_contact_link_restored", user.subject, {"link_id": str(link_id)})
    db.commit()
    db.refresh(link)
    return link