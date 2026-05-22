
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE cases (
  pseudonymised_case_id UUID PRIMARY KEY,
  local_lab_sample_id TEXT UNIQUE,
  specimen_date DATE,
  geographic_region TEXT,
  case_status TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE tb_interpretation (
  sample_id UUID PRIMARY KEY REFERENCES cases(pseudonymised_case_id),
  species_confirmation TEXT,
  lineage TEXT,
  sublineage TEXT,
  resistance_mutations JSONB,
  predicted_drug_resistance JSONB,
  confidence_score NUMERIC,
  interpretation_summary TEXT
);

CREATE TABLE consensus_sequences (
  sample_id UUID PRIMARY KEY REFERENCES cases(pseudonymised_case_id),
  sequence TEXT,
  length INTEGER
);

CREATE TABLE sequencing_runs (
  run_id TEXT PRIMARY KEY,
  platform TEXT,
  instrument_name TEXT,
  pipeline_version TEXT,
  reference_genome TEXT,
  started_at TIMESTAMP,
  completed_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE sample_qc_metrics (
  sample_id UUID PRIMARY KEY REFERENCES cases(pseudonymised_case_id),
  run_id TEXT REFERENCES sequencing_runs(run_id),
  mean_depth NUMERIC,
  coverage_breadth NUMERIC,
  ambiguous_base_percent NUMERIC,
  contamination_flag BOOLEAN DEFAULT FALSE,
  qc_status TEXT,
  qc_failure_reason TEXT,
  reported_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE analysis_provenance (
  provenance_id SERIAL PRIMARY KEY,
  sample_id UUID REFERENCES cases(pseudonymised_case_id),
  pipeline_name TEXT,
  pipeline_version TEXT,
  reference_genome TEXT,
  software_versions JSONB,
  parameters JSONB,
  generated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE clusters (
  cluster_id UUID PRIMARY KEY,
  snp_distance INTEGER,
  investigation_status TEXT,
  alert_flag BOOLEAN
);

CREATE TABLE case_clusters (
  sample_id UUID REFERENCES cases(pseudonymised_case_id),
  cluster_id UUID REFERENCES clusters(cluster_id),
  PRIMARY KEY (sample_id, cluster_id)
);

CREATE TABLE audit_log (
  audit_id SERIAL PRIMARY KEY,
  action TEXT,
  user_id TEXT,
  details JSONB,
  timestamp TIMESTAMP
);

-- --- Pipeline validation sign-offs --------------------------------------------
-- Records formal reviewer sign-offs for pipeline validation steps.
-- Each row is an immutable record; the latest row per pipeline determines current status.
CREATE TABLE IF NOT EXISTS pipeline_validation_signoffs (
  id                SERIAL PRIMARY KEY,
  pipeline          TEXT NOT NULL DEFAULT 'resistance_validation',
  decision          TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'under_review')),
  reviewer          TEXT NOT NULL,
  notes             TEXT,
  catalogue_version TEXT,
  signed_off_at     TIMESTAMP DEFAULT NOW()
);

-- --- Cluster Investigation Centre ---------------------------------------------
-- One row per cluster under active or completed investigation.
CREATE TABLE IF NOT EXISTS cluster_investigations (
  investigation_id  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  cluster_id        UUID NOT NULL REFERENCES clusters(cluster_id) ON DELETE CASCADE,
  risk_score        NUMERIC NOT NULL DEFAULT 0,
  risk_band         TEXT NOT NULL DEFAULT 'low' CHECK (risk_band IN ('low', 'medium', 'high', 'critical')),
  assigned_to       TEXT,
  status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'under_review', 'signed_off', 'closed')),
  epi_notes         TEXT,
  actions           JSONB NOT NULL DEFAULT '[]',
  decision          TEXT,
  decision_by       TEXT,
  decision_at       TIMESTAMP,
  created_at        TIMESTAMP DEFAULT NOW(),
  updated_at        TIMESTAMP DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_cluster_investigations_cluster
  ON cluster_investigations (cluster_id);

-- Structured epidemiology tables
-- These tables hold exposure, contact, and location evidence separately from
-- genomic case records so epidemiological corroboration can be queried and
-- audited instead of being kept only in free-text investigation notes.
CREATE TABLE IF NOT EXISTS contacts (
  contact_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  contact_label TEXT NOT NULL,
  contact_type TEXT,
  relationship_type TEXT,
  pseudonymised_identifier TEXT,
  entered_in_error BOOLEAN NOT NULL DEFAULT FALSE,
  entered_in_error_at TIMESTAMP,
  entered_in_error_by TEXT,
  entered_in_error_reason TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS locations (
  location_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  location_name TEXT NOT NULL,
  location_type TEXT,
  address_line TEXT,
  geographic_region TEXT,
  postcode_prefix TEXT,
  entered_in_error BOOLEAN NOT NULL DEFAULT FALSE,
  entered_in_error_at TIMESTAMP,
  entered_in_error_by TEXT,
  entered_in_error_reason TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS exposures (
  exposure_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  exposure_type TEXT NOT NULL,
  exposure_context TEXT,
  exposure_start_date DATE,
  exposure_end_date DATE,
  confidence TEXT CHECK (confidence IS NULL OR confidence IN ('low', 'medium', 'high')),
  source TEXT,
  notes TEXT,
  entered_in_error BOOLEAN NOT NULL DEFAULT FALSE,
  entered_in_error_at TIMESTAMP,
  entered_in_error_by TEXT,
  entered_in_error_reason TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS case_location_events (
  event_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  case_id UUID NOT NULL REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
  location_id UUID NOT NULL REFERENCES locations(location_id) ON DELETE CASCADE,
  exposure_id UUID REFERENCES exposures(exposure_id) ON DELETE SET NULL,
  event_type TEXT NOT NULL,
  arrived_at TIMESTAMP,
  departed_at TIMESTAMP,
  confidence TEXT CHECK (confidence IS NULL OR confidence IN ('low', 'medium', 'high')),
  source TEXT,
  notes TEXT,
  entered_in_error BOOLEAN NOT NULL DEFAULT FALSE,
  entered_in_error_at TIMESTAMP,
  entered_in_error_by TEXT,
  entered_in_error_reason TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS case_contact_links (
  link_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  case_id UUID NOT NULL REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
  contact_id UUID NOT NULL REFERENCES contacts(contact_id) ON DELETE CASCADE,
  exposure_id UUID REFERENCES exposures(exposure_id) ON DELETE SET NULL,
  link_type TEXT,
  exposure_start_date DATE,
  exposure_end_date DATE,
  source TEXT,
  confidence TEXT CHECK (confidence IS NULL OR confidence IN ('low', 'medium', 'high')),
  notes TEXT,
  entered_in_error BOOLEAN NOT NULL DEFAULT FALSE,
  entered_in_error_at TIMESTAMP,
  entered_in_error_by TEXT,
  entered_in_error_reason TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_locations_region ON locations (geographic_region);
CREATE INDEX IF NOT EXISTS ix_exposures_type ON exposures (exposure_type);
CREATE INDEX IF NOT EXISTS ix_case_location_events_case ON case_location_events (case_id);
CREATE INDEX IF NOT EXISTS ix_case_location_events_location ON case_location_events (location_id);
CREATE INDEX IF NOT EXISTS ix_case_contact_links_case ON case_contact_links (case_id);
CREATE INDEX IF NOT EXISTS ix_case_contact_links_contact ON case_contact_links (contact_id);

-- Reviewer labels for inferred case-pair transmission links
CREATE TABLE IF NOT EXISTS case_pair_reviews (
  case_a UUID NOT NULL REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
  case_b UUID NOT NULL REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
  reviewer_classification TEXT NOT NULL CHECK (
    reviewer_classification IN (
      'confirmed transmission',
      'probable transmission',
      'possible transmission',
      'unlikely transmission',
      'insufficient evidence'
    )
  ),
  reviewer TEXT NOT NULL,
  notes TEXT,
  source_cluster_id UUID REFERENCES clusters(cluster_id) ON DELETE SET NULL,
  entered_in_error BOOLEAN NOT NULL DEFAULT FALSE,
  entered_in_error_at TIMESTAMP,
  entered_in_error_by TEXT,
  entered_in_error_reason TEXT,
  reviewed_at TIMESTAMP DEFAULT NOW(),
  PRIMARY KEY (case_a, case_b),
  CHECK (case_a <> case_b)
);

CREATE INDEX IF NOT EXISTS ix_case_pair_reviews_cluster ON case_pair_reviews (source_cluster_id);
CREATE INDEX IF NOT EXISTS ix_case_pair_reviews_reviewed_at ON case_pair_reviews (reviewed_at DESC);

-- Normalized TB-Profiler/Mykrobe resistance calls.
-- This preserves per-drug/per-mutation evidence rather than only the compact
-- sample-level summary kept in tb_interpretation.
CREATE TABLE IF NOT EXISTS resistance_calls (
  call_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  sample_id UUID NOT NULL REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
  drug TEXT NOT NULL,
  gene TEXT,
  mutation TEXT,
  prediction TEXT,
  confidence NUMERIC,
  depth NUMERIC,
  alt_fraction NUMERIC,
  lineage TEXT,
  source_tool TEXT NOT NULL DEFAULT 'tbprofiler',
  tool_version TEXT,
  database_version TEXT,
  source_path TEXT,
  raw_call JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMP DEFAULT NOW(),
  UNIQUE (sample_id, drug, gene, mutation, source_tool)
);

CREATE INDEX IF NOT EXISTS ix_resistance_calls_sample ON resistance_calls (sample_id);
CREATE INDEX IF NOT EXISTS ix_resistance_calls_drug_prediction ON resistance_calls (drug, prediction);
CREATE INDEX IF NOT EXISTS ix_resistance_calls_source ON resistance_calls (source_tool);

-- Operational alerts raised by rule-based surveillance logic.
CREATE TABLE IF NOT EXISTS alerts (
  alert_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  alert_type TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('info', 'low', 'medium', 'high', 'critical')),
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
  sample_id UUID REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
  cluster_id UUID REFERENCES clusters(cluster_id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT,
  evidence JSONB NOT NULL DEFAULT '{}',
  assigned_to TEXT,
  acknowledged_by TEXT,
  acknowledged_at TIMESTAMP,
  resolved_by TEXT,
  resolved_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_alerts_status ON alerts (status);
CREATE INDEX IF NOT EXISTS ix_alerts_type ON alerts (alert_type);
CREATE INDEX IF NOT EXISTS ix_alerts_cluster ON alerts (cluster_id);
CREATE INDEX IF NOT EXISTS ix_alerts_sample ON alerts (sample_id);

-- Action tracker rows linked to alerts, clusters, or individual cases.
CREATE TABLE IF NOT EXISTS actions (
  action_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  alert_id UUID REFERENCES alerts(alert_id) ON DELETE SET NULL,
  cluster_id UUID REFERENCES clusters(cluster_id) ON DELETE SET NULL,
  sample_id UUID REFERENCES cases(pseudonymised_case_id) ON DELETE SET NULL,
  action_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'completed', 'cancelled')),
  owner TEXT,
  note TEXT,
  due_at TIMESTAMP,
  completed_at TIMESTAMP,
  created_by TEXT,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_actions_status ON actions (status);
CREATE INDEX IF NOT EXISTS ix_actions_alert ON actions (alert_id);
CREATE INDEX IF NOT EXISTS ix_actions_cluster ON actions (cluster_id);
CREATE INDEX IF NOT EXISTS ix_actions_sample ON actions (sample_id);

-- Additional hot-query path indexes for synthesis and reporting
CREATE INDEX IF NOT EXISTS ix_case_clusters_cluster ON case_clusters (cluster_id);
CREATE INDEX IF NOT EXISTS ix_audit_log_action ON audit_log (action);
CREATE INDEX IF NOT EXISTS ix_audit_log_timestamp ON audit_log (timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_analysis_provenance_sample ON analysis_provenance (sample_id);
