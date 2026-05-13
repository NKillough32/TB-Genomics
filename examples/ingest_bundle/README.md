# Ingest Example Bundle

This folder contains ingest-ready example files so users can see exactly how to shape and augment their own data before loading into the platform.

## Why this exists

- Show required columns and value formats.
- Demonstrate key links across files.
- Provide a safe synthetic example using public online incidence trends.

## Files in this bundle

- `cases.csv`: core case table rows.
- `tb_interpretation.csv`: lineage/resistance outputs linked by `sample_id`.
- `sequencing_runs.csv`: run-level metadata.
- `sample_qc_metrics.csv`: QC metrics linked by `sample_id` and `run_id`.
- `analysis_provenance.csv`: pipeline metadata and parameters.
- `dna.fasta`: consensus sequences with FASTA headers matching `sample_id`.
- `ingest_manifest.json`: generation metadata and file purpose.
- `load_example_data.sql`: SQL template for loading CSV files into PostgreSQL.

## Key linkage rules (most important)

1. `cases.pseudonymised_case_id` must match:
   - `tb_interpretation.sample_id`
   - `sample_qc_metrics.sample_id`
   - `analysis_provenance.sample_id`
   - FASTA header IDs in `dna.fasta`
2. `sample_qc_metrics.run_id` must exist in `sequencing_runs.run_id`.
3. Dates should be ISO format where possible (for example `2026-05-01`).
4. JSON columns should be valid JSON strings in CSV.

## Generate a fresh example bundle

From project root:

```bash
python scripts/generate_ingest_example_bundle.py --cases 40 --output-dir examples/ingest_bundle
```

The generator uses public World Bank TB incidence data (`SH.TBS.INCD`) to create realistic country weighting.

## How to augment your own data

1. Start by mapping your internal columns into `cases.csv` shape.
2. Add lineage/resistance outputs into `tb_interpretation.csv`.
3. Add run metadata and QC data (`sequencing_runs.csv`, `sample_qc_metrics.csv`).
4. Add provenance records for reproducibility (`analysis_provenance.csv`).
5. Ensure FASTA headers match your sample IDs exactly.
6. Validate joins by checking every sample in interpretation/QC/provenance exists in cases.

## Load into database (local example)

Use `load_example_data.sql` as a template with `psql`.

On Windows PowerShell, use:

```powershell
psql -f .\examples\ingest_bundle\load_example_data.sql
```

## Note about API ingest endpoint

`POST /ingest/file` uploads files to `uploads/` but does not automatically parse or insert them into the database.

For NI live data, use the full ingest pipeline instead:

1. **Prepare** — map NI export columns to bundle format:

   ```powershell
   python scripts/prepare_ni_data.py --config scripts/ni_column_map.json --list-columns
   python scripts/prepare_ni_data.py --config scripts/ni_column_map.json --out path/to/bundle
   ```

2. **Validate** — check bundle compliance before loading:

   ```powershell
   python scripts/validate_ingest_files.py --dir path/to/bundle
   ```

3. **Load** — idempotent DB insert (safe to re-run):

   ```powershell
   python scripts/load_ingest_bundle.py --dir path/to/bundle --dry-run
   python scripts/load_ingest_bundle.py --dir path/to/bundle
   ```

Edit `scripts/ni_column_map.json` to match actual NI export column names before running `prepare_ni_data.py`.
Use `--reset --confirm-reset` with `load_ingest_bundle.py` to truncate all tables before a fresh load (requires both flags).
