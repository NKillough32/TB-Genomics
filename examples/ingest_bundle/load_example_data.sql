-- Example loader for ingest bundle CSV files.
-- Run from project root using:
--   psql -f ./examples/ingest_bundle/load_example_data.sql

\set ON_ERROR_STOP on

BEGIN;

-- Load parent tables first.
\copy cases(pseudonymised_case_id, local_lab_sample_id, specimen_date, geographic_region, case_status) FROM './examples/ingest_bundle/cases.csv' DELIMITER ',' CSV HEADER;

\copy sequencing_runs(run_id, platform, instrument_name, pipeline_version, reference_genome, started_at, completed_at) FROM './examples/ingest_bundle/sequencing_runs.csv' DELIMITER ',' CSV HEADER;

-- Load child tables with FK dependencies.
\copy tb_interpretation(sample_id, species_confirmation, lineage, sublineage, resistance_mutations, predicted_drug_resistance, confidence_score, interpretation_summary) FROM './examples/ingest_bundle/tb_interpretation.csv' DELIMITER ',' CSV HEADER;

\copy sample_qc_metrics(sample_id, run_id, mean_depth, coverage_breadth, ambiguous_base_percent, contamination_flag, qc_status, qc_failure_reason, reported_at) FROM './examples/ingest_bundle/sample_qc_metrics.csv' DELIMITER ',' CSV HEADER;

\copy analysis_provenance(sample_id, pipeline_name, pipeline_version, reference_genome, software_versions, parameters, generated_at) FROM './examples/ingest_bundle/analysis_provenance.csv' DELIMITER ',' CSV HEADER;

COMMIT;

