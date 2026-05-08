
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

CREATE TABLE clusters (
  cluster_id UUID PRIMARY KEY,
  snp_distance INTEGER,
  investigation_status TEXT,
  alert_flag BOOLEAN
);

CREATE TABLE case_clusters (
  sample_id UUID REFERENCES cases(pseudonymised_case_id),
  cluster_id UUID REFERENCES clusters(cluster_id)
);

CREATE TABLE audit_log (
  audit_id SERIAL PRIMARY KEY,
  action TEXT,
  user_id TEXT,
  details JSONB,
  timestamp TIMESTAMP
);
