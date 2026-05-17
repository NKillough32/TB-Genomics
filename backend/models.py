
from sqlalchemy import Column, Date, ForeignKey, String, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from backend.database import Base
import uuid

class Case(Base):
    __tablename__ = "cases"
    pseudonymised_case_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    local_lab_sample_id = Column(String, unique=True)
    specimen_date = Column(Date)
    geographic_region = Column(String)
    case_status = Column(String)
    created_at = Column(TIMESTAMP)


class Contact(Base):
    __tablename__ = "contacts"

    contact_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_label = Column(String, nullable=False)
    contact_type = Column(String)
    relationship_type = Column(String)
    pseudonymised_identifier = Column(String)
    created_at = Column(TIMESTAMP)


class Location(Base):
    __tablename__ = "locations"

    location_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    location_name = Column(String, nullable=False)
    location_type = Column(String)
    address_line = Column(String)
    geographic_region = Column(String)
    postcode_prefix = Column(String)
    created_at = Column(TIMESTAMP)


class Exposure(Base):
    __tablename__ = "exposures"

    exposure_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    exposure_type = Column(String, nullable=False)
    exposure_context = Column(String)
    exposure_start_date = Column(Date)
    exposure_end_date = Column(Date)
    confidence = Column(String)
    source = Column(String)
    notes = Column(String)
    created_at = Column(TIMESTAMP)


class CaseLocationEvent(Base):
    __tablename__ = "case_location_events"

    event_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.pseudonymised_case_id"), nullable=False)
    location_id = Column(UUID(as_uuid=True), ForeignKey("locations.location_id"), nullable=False)
    exposure_id = Column(UUID(as_uuid=True), ForeignKey("exposures.exposure_id"))
    event_type = Column(String, nullable=False)
    arrived_at = Column(TIMESTAMP)
    departed_at = Column(TIMESTAMP)
    confidence = Column(String)
    source = Column(String)
    notes = Column(String)
    created_at = Column(TIMESTAMP)


class CaseContactLink(Base):
    __tablename__ = "case_contact_links"

    link_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.pseudonymised_case_id"), nullable=False)
    contact_id = Column(UUID(as_uuid=True), ForeignKey("contacts.contact_id"), nullable=False)
    exposure_id = Column(UUID(as_uuid=True), ForeignKey("exposures.exposure_id"))
    link_type = Column(String)
    exposure_start_date = Column(Date)
    exposure_end_date = Column(Date)
    source = Column(String)
    confidence = Column(String)
    notes = Column(String)
    created_at = Column(TIMESTAMP)
