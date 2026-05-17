from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.orm import Session
from typing import Annotated

from backend.auth import AuthenticatedUser, require_roles
from backend.models import CaseContactLink, CaseLocationEvent, Contact, Exposure, Location
from backend.routers.case_overview import get_db


router = APIRouter(prefix="/epidemiology", tags=["epidemiology"])

Confidence = Annotated[str, StringConstraints(strip_whitespace=True, pattern="^(low|medium|high)$")]


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
    created_at: datetime | None = None


class ContactCreate(BaseModel):
    contact_label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    contact_type: str | None = None
    relationship_type: str | None = None
    pseudonymised_identifier: str | None = None


class ContactOut(ContactCreate):
    model_config = ConfigDict(from_attributes=True)

    contact_id: UUID
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
    created_at: datetime | None = None


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
    created_at: datetime | None = None


def _get_or_404(db: Session, model, primary_key: UUID, label: str):
    item = db.get(model, primary_key)
    if item is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return item


@router.get("/exposures", response_model=list[ExposureOut])
def list_exposures(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return db.query(Exposure).order_by(Exposure.created_at.desc()).offset(offset).limit(limit).all()


@router.post("/exposures", response_model=ExposureOut)
def create_exposure(
    payload: ExposureCreate,
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_roles("operator")),
):
    exposure = Exposure(**payload.model_dump())
    db.add(exposure)
    db.commit()
    db.refresh(exposure)
    return exposure


@router.get("/exposures/{exposure_id}", response_model=ExposureOut)
def get_exposure(exposure_id: UUID, db: Session = Depends(get_db)):
    return _get_or_404(db, Exposure, exposure_id, "Exposure")


@router.get("/contacts", response_model=list[ContactOut])
def list_contacts(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return db.query(Contact).order_by(Contact.created_at.desc()).offset(offset).limit(limit).all()


@router.post("/contacts", response_model=ContactOut)
def create_contact(
    payload: ContactCreate,
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_roles("operator")),
):
    contact = Contact(**payload.model_dump())
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


@router.get("/contacts/{contact_id}", response_model=ContactOut)
def get_contact(contact_id: UUID, db: Session = Depends(get_db)):
    return _get_or_404(db, Contact, contact_id, "Contact")


@router.get("/locations", response_model=list[LocationOut])
def list_locations(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return db.query(Location).order_by(Location.created_at.desc()).offset(offset).limit(limit).all()


@router.post("/locations", response_model=LocationOut)
def create_location(
    payload: LocationCreate,
    db: Session = Depends(get_db),
    _user: AuthenticatedUser = Depends(require_roles("operator")),
):
    location = Location(**payload.model_dump())
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


@router.get("/locations/{location_id}", response_model=LocationOut)
def get_location(location_id: UUID, db: Session = Depends(get_db)):
    return _get_or_404(db, Location, location_id, "Location")


@router.get("/case-location-events", response_model=list[CaseLocationEventOut])
def list_case_location_events(
    case_id: UUID | None = None,
    location_id: UUID | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(CaseLocationEvent)
    if case_id:
        query = query.filter(CaseLocationEvent.case_id == case_id)
    if location_id:
        query = query.filter(CaseLocationEvent.location_id == location_id)
    return query.order_by(CaseLocationEvent.created_at.desc()).offset(offset).limit(limit).all()


@router.get("/case-contact-links", response_model=list[CaseContactLinkOut])
def list_case_contact_links(
    case_id: UUID | None = None,
    contact_id: UUID | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(CaseContactLink)
    if case_id:
        query = query.filter(CaseContactLink.case_id == case_id)
    if contact_id:
        query = query.filter(CaseContactLink.contact_id == contact_id)
    return query.order_by(CaseContactLink.created_at.desc()).offset(offset).limit(limit).all()
