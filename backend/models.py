
from sqlalchemy import Boolean, Column, Date, ForeignKey, Integer, Numeric, String, TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB, UUID
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


class TbInterpretation(Base):
    __tablename__ = "tb_interpretation"

    sample_id = Column(UUID(as_uuid=True), ForeignKey("cases.pseudonymised_case_id"), primary_key=True)
    species_confirmation = Column(String)
    lineage = Column(String)
    sublineage = Column(String)
    resistance_mutations = Column(JSONB)
    predicted_drug_resistance = Column(JSONB)
    confidence_score = Column(Numeric)
    interpretation_summary = Column(String)


class ConsensusSequence(Base):
    __tablename__ = "consensus_sequences"

    sample_id = Column(UUID(as_uuid=True), ForeignKey("cases.pseudonymised_case_id"), primary_key=True)
    sequence = Column(String)
    length = Column(Integer)


class SequencingRun(Base):
    __tablename__ = "sequencing_runs"

    run_id = Column(String, primary_key=True)
    platform = Column(String)
    instrument_name = Column(String)
    pipeline_version = Column(String)
    reference_genome = Column(String)
    started_at = Column(TIMESTAMP)
    completed_at = Column(TIMESTAMP)
    created_at = Column(TIMESTAMP)


class SampleQcMetric(Base):
    __tablename__ = "sample_qc_metrics"

    sample_id = Column(UUID(as_uuid=True), ForeignKey("cases.pseudonymised_case_id"), primary_key=True)
    run_id = Column(String, ForeignKey("sequencing_runs.run_id"))
    mean_depth = Column(Numeric)
    coverage_breadth = Column(Numeric)
    ambiguous_base_percent = Column(Numeric)
    contamination_flag = Column(Boolean)
    qc_status = Column(String)
    qc_failure_reason = Column(String)
    reported_at = Column(TIMESTAMP)


class AnalysisProvenance(Base):
    __tablename__ = "analysis_provenance"

    provenance_id = Column(Integer, primary_key=True)
    sample_id = Column(UUID(as_uuid=True), ForeignKey("cases.pseudonymised_case_id"))
    pipeline_name = Column(String)
    pipeline_version = Column(String)
    reference_genome = Column(String)
    software_versions = Column(JSONB)
    parameters = Column(JSONB)
    generated_at = Column(TIMESTAMP)


class Cluster(Base):
    __tablename__ = "clusters"

    cluster_id = Column(UUID(as_uuid=True), primary_key=True)
    snp_distance = Column(Integer)
    investigation_status = Column(String)
    alert_flag = Column(Boolean)


class CaseCluster(Base):
    __tablename__ = "case_clusters"

    sample_id = Column(UUID(as_uuid=True), ForeignKey("cases.pseudonymised_case_id"), primary_key=True)
    cluster_id = Column(UUID(as_uuid=True), ForeignKey("clusters.cluster_id"), primary_key=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    audit_id = Column(Integer, primary_key=True)
    action = Column(String)
    user_id = Column(String)
    details = Column(JSONB)
    timestamp = Column(TIMESTAMP)


class PipelineValidationSignoff(Base):
    __tablename__ = "pipeline_validation_signoffs"

    id = Column(Integer, primary_key=True)
    pipeline = Column(String, nullable=False)
    decision = Column(String, nullable=False)
    reviewer = Column(String, nullable=False)
    notes = Column(String)
    catalogue_version = Column(String)
    signed_off_at = Column(TIMESTAMP)


class ClusterInvestigation(Base):
    __tablename__ = "cluster_investigations"

    investigation_id = Column(UUID(as_uuid=True), primary_key=True)
    cluster_id = Column(UUID(as_uuid=True), ForeignKey("clusters.cluster_id"), nullable=False)
    risk_score = Column(Numeric, nullable=False)
    risk_band = Column(String, nullable=False)
    assigned_to = Column(String)
    status = Column(String, nullable=False)
    epi_notes = Column(String)
    actions = Column(JSONB, nullable=False)
    decision = Column(String)
    decision_by = Column(String)
    decision_at = Column(TIMESTAMP)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)


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


class CasePairReview(Base):
    __tablename__ = "case_pair_reviews"

    case_a = Column(UUID(as_uuid=True), ForeignKey("cases.pseudonymised_case_id"), primary_key=True)
    case_b = Column(UUID(as_uuid=True), ForeignKey("cases.pseudonymised_case_id"), primary_key=True)
    reviewer_classification = Column(String, nullable=False)
    reviewer = Column(String, nullable=False)
    notes = Column(String)
    source_cluster_id = Column(UUID(as_uuid=True), ForeignKey("clusters.cluster_id"))
    reviewed_at = Column(TIMESTAMP)
