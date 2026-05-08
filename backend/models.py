
from sqlalchemy import Column, String, Date, JSON, TIMESTAMP
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
