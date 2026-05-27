
TB Genomic Surveillance Platform
===============================

This project helps TB teams turn sequencing-linked case data into clear operational insight.

It supports day-to-day surveillance by:
- identifying potential genomic clusters
- tracking programme performance (coverage, QC, turnaround)
- prioritizing where follow-up is most urgent
- producing repeatable investigation reports for review
- combining multiple analytic outputs into a single investigation summary for public-health review

Current capabilities include:
- FastAPI backend APIs for cases, ingest, KPIs, jobs, and reporting
- Button-driven GUI workflow grouped into Prepare, Analyse, Investigate, and Report/Govern phases
- Web interface for operational use
- PostgreSQL data model for surveillance and WGS reporting
- Outbreaker2 integration for analyst-led outbreak analysis
- A synthesis layer that sits between analytics and investigation and turns raw outputs into review-ready summaries
- Cluster Investigation Centre for assigning reviewers, recording epidemiology evidence, logging actions, and signing off investigations
- Optional epidemiology reference library for reusable exposure, contact, and location records
- TBProfiler + Mykrobe integration (WSL / Docker fallback) for lineage and drug resistance calling
- Parallel dual-tool DR concordance checking with discordance flagged in audit_log
- Automated alerts and actions system for surveillance rule-based event generation
- Normalized drug resistance calls with per-drug/per-mutation evidence tracking
- FASTQ discovery and validation for direct raw-read analysis
- Reproducible TB WGS pipeline contract under `pipelines/tb_wgs/`
- NI data ingest pipeline: prepare_ni_data.py, validate_ingest_files.py, load_ingest_bundle.py
- Governance/setup documentation for secure deployment and integration

Plain-language note for public health users:
- The synthesis layer does not make decisions for you.
- It combines genomic, timing, geography, resistance, and model signals into a simple interpretation.
- It marks contradictions and missing data so reviewers can decide what needs follow-up.
- Its scores are heuristic and non-validated, so they should be used for triage and review support only.

Plain-language project overview
------------------------------

For a simple explanation of what this project does and how it supports TB programme operations,
see: docs/PROJECT_EXPLAINED_SIMPLE.md

For governance and how the platform should be used safely in public health settings, see:
- docs/governance/README.md

For ingest examples and data shape guidance, see:
- examples/ingest_bundle/README.md

External dependencies (not bundled):
- PostgreSQL
- Python >= 3.10
- R >= 4.1 with outbreaker2

Repository hygiene note:
- `R_libs/` is intentionally not tracked in git. Treat it as a local package cache only.
- Restore R dependencies in your own R environment, or use the existing WSL / Docker fallbacks for supported analysis steps.

Quick start:
1) Create and activate a virtual environment:
	python -m venv .venv
	.\.venv\Scripts\Activate.ps1
2) Install Python dependencies:
	pip install -r backend/requirements.txt
3) Create the PostgreSQL user and database if they do not already exist:
	& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -d postgres
	Then run inside the `psql` prompt:
	CREATE USER tb WITH PASSWORD 'tb';
	CREATE DATABASE tb_surveillance OWNER tb;
	GRANT ALL PRIVILEGES ON DATABASE tb_surveillance TO tb;
	\q
4) Load the schema from PowerShell (not from inside the `psql` prompt):
	& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U tb -h localhost -d tb_surveillance -f ".\db\schema.sql"
5) Set the database connection string for the current PowerShell session:
	$env:DATABASE_URL="postgresql://tb:tb@localhost/tb_surveillance"
6) Start the backend:
	c:/Users/Nicho/Desktop/TB-Genomics-main/.venv/Scripts/python.exe -m uvicorn backend.app:app --reload
7) Start the GUI from a second PowerShell window:
	cd gui
	python -m http.server 8081
8) Open http://localhost:8081

One-click Windows launchers:
- Double-click `Start-Backend.bat` to run only the backend.
- Double-click `Start-Platform.bat` to run backend + GUI and open the browser.
- Double-click `Start-Platform-TBTools.bat` to run backend + GUI with TBProfiler/Mykrobe fallback checks enabled and a WSL tool window.
- Double-click `Setup-And-Start-Platform.bat` for first-time setup (creates `.venv`, installs dependencies, then starts backend + GUI).
- Double-click `Start-Backend-Demo.bat` for backend in demo mode (requires typing DEMO confirmation).
- Double-click `Start-Platform-Demo.bat` for backend + GUI in demo mode (requires typing DEMO confirmation).

Notes:
- These launchers expect `.venv` to already exist with dependencies installed.
- If `DATABASE_URL` is not set, the scripts default to:
	`postgresql://tb:tb@localhost/tb_surveillance`
- `Start-Platform-TBTools.bat` enables `TBPROFILER_WSL_FALLBACK=1`, `TBPROFILER_DOCKER_FALLBACK=1`, and `TBPROFILER_WSL_ENV=tbtools` for that session, opens a WSL shell to verify TBProfiler/Mykrobe availability, and refreshes `exports/lineage_dr_validation.json` before opening the browser.
- Demo launchers set `TB_ENABLE_SYNTHETIC_SEEDING=1` and `TB_ALLOW_NON_OPERATIONAL_ACTIONS=1` for that session only.

GUI workflow overview
---------------------

The web interface is organised around the operational workflow:

1. **Prepare** - check system status, data safety, data readiness, uploads, and demo/synthetic data controls.
2. **Analyse** - run the full pipeline or individual analysis jobs, then review raw results, exports, case search, and case reports.
3. **Investigate** - review transmission synthesis, open the Cluster Investigation Centre, record case evidence, actions, and sign-off decisions, and inspect visual analytics.
4. **Report and govern** - generate the final actionable report, inspect runtime/KPI status, record resistance-validation sign-off, manage reusable epidemiology reference records, and view the audit trail.

Important Step 6 / Step 11 distinction:

- **Step 6 - Cluster Investigation Centre** is where investigators attach evidence to cases. Load clusters, open a cluster, choose the **Epi notes** tab, select a case, then record a location event or contact link.
- **Step 11 - Epidemiology Reference Records** is only a reusable reference library for exposure types, contacts, and locations. It does not assign records to cases or clusters.

Windows notes:
- If `CREATE USER tb` reports `role "tb" already exists`, that means the user is already present and you can continue.
- If `CREATE DATABASE tb_surveillance` reports `database "tb_surveillance" already exists`, that means the database is already present and you can continue.
- If you see a `postgres=#` prompt, you are inside the PostgreSQL shell. Exit with `\q` before running PowerShell commands such as `psql ... -f ".\db\schema.sql"`.

Synthetic dataset setup (real public baseline data)
---------------------------------------------------

You can seed a realistic but fake case-level dataset using public TB incidence data from
the World Bank indicator API (`SH.TBS.INCD`).

Option A: Seed from command line
1) From the project root, run:
	python -m backend.synthetic_seed --cases 250 --reset
2) The script inserts synthetic records into:
	- `cases`
	- `tb_interpretation`
	- `consensus_sequences`
	- `sequencing_runs`
	- `sample_qc_metrics`
	- `analysis_provenance`
	- `clusters`
	- `case_clusters`
	- `audit_log`

Option B: Seed via API
1) Ensure backend is running.
2) Call:
	POST http://localhost:8000/ingest/seed-synthetic?case_count=250&reset=true&seed=42

Notes:
- Data is pseudonymised and synthetic at case level.
- Country weighting is based on latest online incidence values.
- If the online fetch fails, the seeder uses embedded fallback incidence values.

Data safety policy: demo mode vs operational mode
-------------------------------------------------

To prevent accidental use of synthetic/demo records for real public-health action,
the platform now applies safety gates by default.

Default behavior:
- Synthetic seeding endpoint is disabled.
- Full pipeline execution is blocked when synthetic/demo records are detected.
- Outbreak report generation and bulk export are blocked when dataset is non-operational.

Demo mode (for training/presentations only):
1) Enable synthetic seeding:
	$env:TB_ENABLE_SYNTHETIC_SEEDING="1"
2) If you need to run full analysis/reporting on synthetic data, enable override:
	$env:TB_ALLOW_NON_OPERATIONAL_ACTIONS="1"

Operational mode (real programme use):
- Do not set `TB_ALLOW_NON_OPERATIONAL_ACTIONS`.
- Keep synthetic seeding disabled (`TB_ENABLE_SYNTHETIC_SEEDING` unset or `0`).
- Confirm dataset safety before running actions:
	GET /cases/data-safety
- Confirm data readiness before analysis/reporting:
	GET /cases/data-readiness

Expected status values:
- `operational_safe: true` means actions are allowed.
- `operational_safe: false` means synthetic/demo signals were detected and sensitive actions are blocked.
- `data-readiness.status: ready` means all required completeness checks are populated.
- `data-readiness.status: needs_review` means at least one readiness check has missing values.

Recommended startup profiles
----------------------------

Operational profile (production-like):
1) Ensure these are NOT enabled:
	Remove-Item Env:TB_ENABLE_SYNTHETIC_SEEDING -ErrorAction SilentlyContinue
	Remove-Item Env:TB_ALLOW_NON_OPERATIONAL_ACTIONS -ErrorAction SilentlyContinue
2) Start backend and GUI.
3) Verify:
	GET http://localhost:8000/cases/data-safety

Demonstration profile:
1) Set explicit demo flags:
	$env:TB_ENABLE_SYNTHETIC_SEEDING="1"
	$env:TB_ALLOW_NON_OPERATIONAL_ACTIONS="1"
2) Seed synthetic dataset from UI or API.
3) Run pipeline and generate reports for demonstration.
4) Clear demo flags before any operational run.

Azure VM sequencing integration
-------------------------------

If sequencing output is generated on an Azure VM and needs to feed this platform,
use the connector script and setup guide:

- scripts/azure_vm_ingest_connector.py
- docs/governance/AZURE_VM_INGEST_SETUP.md

The connector supports scheduled uploads and only sends new/changed files.

To enforce ingest authentication, set TB_INGEST_API_KEY on the backend host and configure
TB_API_KEY with the same value in the VM connector environment.

`POST /ingest/file` still stores single uploaded files under `uploads/`. When the uploaded
file is a ZIP ingest bundle containing `cases.csv`, the endpoint now extracts it safely,
validates it with `scripts/validate_ingest_files.py`, and loads it through
`scripts/load_ingest_bundle.py` unless `load_to_database=false` or `dry_run=true` is set.


Lineage and drug resistance calling
------------------------------------

Scripts are wired as named jobs and called by the backend job runner.

Full pipeline order:
1. Derive sequence clusters.
2. Export outbreaker inputs (`exports/cases.csv` and `exports/dna.fasta`).
3. Run advanced FASTA analysis.
4. Run lineage and drug-resistance validation against the active FASTA.
5. Generate rule-based alerts.
6. Run outbreaker2.
7. Compare clustering methods.
8. Run transmission synthesis.

Advanced FASTA analysis:
- Job name: `run_fasta_analysis`.
- Uses `FASTA_ANALYSIS_FASTA` / `TB_FASTA_ANALYSIS_FASTA` when set, otherwise `exports/dna.fasta` or the newest FASTA in `exports/` or `uploads/`.
- Detects and runs optional external tools when available: SeqKit, SNP-sites, snp-dists, IQ-TREE, and TreeTime.
- Uses local executables first, then WSL micromamba fallback when `FASTA_ANALYSIS_WSL_FALLBACK=1` (default) and `FASTA_ANALYSIS_WSL_ENV` / `TBPROFILER_WSL_ENV` points to the tool environment.
- Writes `exports/fasta_analysis_summary.json` plus tool outputs under `exports/fasta_analysis/`.
- Missing tools are recorded as warnings rather than failing the whole workflow.
- Useful outputs include FASTA statistics, extracted SNP-site alignment, an external SNP distance matrix, a maximum-likelihood tree, and a dated tree when IQ-TREE and TreeTime are both available.

WSL tool install example:

```
micromamba install -y -n tbtools -c conda-forge -c bioconda seqkit snp-sites snp-dists iqtree treetime
```

TBProfiler (primary engine):
- Uses the active FASTA selected by `LINEAGE_DR_FASTA` / `TB_LINEAGE_DR_FASTA`, or `exports/dna.fasta` from the current pipeline run.
- Tries the local executable when usable; otherwise uses WSL (`tbtools` mamba env) and then Docker when enabled and available.
- Output artifact: `exports/tbprofiler/` and imported into `tb_interpretation`.

Mykrobe (secondary engine):
- Runs via WSL whenever available and the active FASTA sample IDs validate against cases.
- Output artifact: `exports/mykrobe/<sample_id>_mykrobe.json`.
- Results imported first; TBProfiler results overwrite as authoritative source of truth.
- Import order: Mykrobe -> TBProfiler (TBProfiler always wins on conflict).

DR concordance checking:
- After both tools run, per-drug R/S calls are compared for each sample.
- Discordant samples (one tool says R, the other says S) are written to `audit_log` with action `dr_concordance_discordance_flagged`.
- Concordance summary included in `exports/lineage_dr_validation.json` under `dr_concordance`.

FASTQ discovery and validation:
- The pipeline automatically discovers FASTQ pairs in `uploads/` or specified input directories.
- FASTQ metadata is extracted and validated against consensus sequences to ensure consistency.
- Supports WSL and Docker fallback environments for TBProfiler and Mykrobe execution on raw reads.
- FASTQ-based runs are logged in `exports/lineage_dr_validation.json` under `fastq_inputs` with per-sample diagnostics.

Containerised WGS pipeline contract:
- The Nextflow workflow skeleton in `pipelines/tb_wgs/main.nf` defines the production raw-read module boundaries: fastp, BWA-MEM, Samtools QC, Bcftools calling, masking, snp-dists, TBProfiler, and manifest generation.
- The Snakemake workflow in `pipelines/tb_wgs/Snakefile` runs the deterministic validation fixture used in CI.
- The validation fixture in `validation/tb_wgs/` covers FASTQ -> QC -> mapping -> variant calling -> masking -> masked FASTA -> SNP distance matrix -> lineage -> resistance calls -> manifest.
- The root validation harness in `validation/run_validation.py` compares expected SNP distances, lineage, drug resistance, QC pass/fail, and cluster assignment.
- CI runs the fixture and compares observed outputs with expected artefacts so output drift is caught before merge.
- The current fixture runner is dependency-free for CI speed; production deployments should replace Nextflow placeholder commands with locked, validated container commands while preserving the same output files.

Environment variables for tool execution:
- `TBPROFILER_WSL_FALLBACK=1` (default on): enables WSL execution path.
- `TBPROFILER_WSL_ENV=tbtools` (default): mamba env name inside WSL.
- `TBPROFILER_DOCKER_FALLBACK=1` (default on): enables Docker fallback if WSL fails.
- `TBPROFILER_DOCKER_IMAGE`: TBProfiler Docker image (default: `quay.io/jodyphelan/tbprofiler:latest`).

NI live data ingest pipeline
-----------------------------

Three scripts handle the end-to-end NI data ingest workflow:

1. **Prepare**: transform NI-format exports into a standard bundle.

	python scripts/prepare_ni_data.py --config scripts/ni_column_map.json --out path/to/bundle

   Use `--list-columns` to discover source column names before editing the mapping.
   Use `--dry-run` to preview first 5 rows without writing.

2. **Validate**: check the bundle for schema compliance.

	python scripts/validate_ingest_files.py --dir path/to/bundle

3. **Load**: idempotent DB load (safe to re-run).

	python scripts/load_ingest_bundle.py --dir path/to/bundle [--dry-run]
	python scripts/load_ingest_bundle.py --dir path/to/bundle --reset --confirm-reset

Notes:
- All inserts use `ON CONFLICT DO NOTHING` - re-running is safe.
- `--reset --confirm-reset` truncates ALL tables before loading; requires both flags to prevent accidents.
- `ni_column_map.json` contains value maps for HSC Trust names, case status, and all column mappings.
- Before first use on live NI data: run `--list-columns` to discover actual column names, then update `source_column` values in `ni_column_map.json`.
- Ensure DB contains no synthetic seed events (`audit_log WHERE action='seed_synthetic_dataset'`) before loading real data.


Normalized drug resistance calls
---------------------------------

The platform normalises and audits drug resistance outputs from specialist tools into structured, queryable records.

**normalise_resistance.py** processes TB-Profiler JSON outputs:

```bash
python scripts/normalise_resistance.py --input exports/tbprofiler/ --dry-run
python scripts/normalise_resistance.py --input exports/tbprofiler/
```

Per-drug calls are stored in the `resistance_calls` table with:
- Drug name and prediction (R/S/U)
- Gene and mutation details with confidence scores
- Lineage, tool version, and database version for provenance
- Raw call artifacts for audit and review

The script runs automatically as part of the full pipeline after TBProfiler/Mykrobe execution. Outputs are stored in `exports/resistance_validation.json` and imported to `resistance_calls` for dashboards and reports.

Alerts and actions system
---------------------------------

Automated rule-based alerts trigger on genomic, resistance, and cluster-growth signals.

**Automatic alert generation:**

```bash
python scripts/generate_alerts.py
```

Alert types include:
- `probable_cluster` (high severity): SNP distance ≤ configurable probable threshold (default 5 SNPs)
- `possible_cluster` (medium severity): SNP distance ≤ configurable possible threshold (default 12 SNPs)
- `dr_resistance_alert` (varies): drug resistance patterns with genomic evidence
- `cluster_growth` (varies): clusters exceeding `TB_CLUSTER_ALERT_MIN_CASES` with rapid membership changes

Alerts are stored in the `alerts` table with:
- Type, severity (info/low/medium/high/critical), and status (open/acknowledged/resolved)
- Associated sample_id or cluster_id
- Title, description, and structured evidence JSONB
- Timestamps and workflow fields for assignment and resolution tracking

Linked actions (`actions` table) allow operators to record tasks, assignments, and completion:
- Action type and status (open/in_progress/completed/cancelled)
- Owner, due date, and completion tracking
- Relationship to alerts, clusters, or samples

Environment variable configuration:
- `TB_ALERT_PROBABLE_SNP` (default 5): SNP threshold for probable clusters
- `TB_ALERT_POSSIBLE_SNP` (default 12): SNP threshold for possible clusters
- `TB_CLUSTER_ALERT_MIN_CASES` (default 5): minimum cluster size to trigger growth alerts

The alerts script runs automatically as part of the pipeline and can also be invoked standalone via API:

```
POST /jobs/run?job_name=generate_alerts
```


The outbreak report is available in two formats:

- GET /cases/outbreak-report for the existing PDF report.
- GET /cases/outbreak-report.html for the short browser-friendly HTML report that is saved to exports/outbreaker_investigation_report.html.
- GET /cases/outbreak-report.full.html for the full browser-friendly HTML report that is saved alongside it as exports/outbreaker_investigation_report_full.html.

Use the short HTML report for online publication workflows after local information-governance review, and use the full HTML report when reviewers need all action/discordance rows and complete JSON source artifacts alongside the summary. Both HTML outputs embed outbreak graphics and use responsive tables to reduce PDF-only wrapping and page-break formatting issues.

Actionable surveillance report
-------------------------------

The actionable surveillance report synthesizes programme-level metrics, recent alerts, pending actions, and operational status into a single dashboard view suitable for leadership and operational review.

**Endpoints:**

- GET /reports/actionable-surveillance - Returns JSON summary with recent alerts, actions, investigation status, and KPI snapshots.
- GET /reports/actionable-surveillance.html - Returns HTML dashboard report suitable for printing or web publication.

The HTML report includes:
- Current data safety and readiness status
- Recent alerts with severity and context
- Pending actions and their owners
- Investigation cluster summaries with risk assessments
- Resistance validation sign-off status
- Historical KPI trends (12-week default)
- Audit trail of recent critical events
- Analysis pipeline provenance and software versions

The report automatically embeds the latest exports from lineage_dr_validation.json, resistance_validation.json, and transmission_synthesis data to provide current operational intelligence.

TB surveillance KPI reporting
-----------------------------

Endpoint:

- GET /cases/kpis?weeks=12

This returns programme-level metrics including sequencing coverage and QC indicators.

Schema additions for WGS reporting:

- sequencing_runs
- sample_qc_metrics
- analysis_provenance

If your database was created before these additions, apply the updated db/schema.sql.
The endpoint remains backward-compatible and returns a warning until QC tables exist.

New synthesis and investigation endpoints
----------------------------------------

These endpoints provide the new review layer between analytics and investigation:

- GET /analytics/transmission-synthesis
- GET /analytics/transmission-synthesis/{cluster_id}
- GET /analytics/cluster-risk-summary

In plain language, these endpoints answer:

- Which cluster should a reviewer look at first?
- Which possible transmission pairs have strong, moderate, or contradictory support?
- What flags suggest missing data, cross-region spread, resistance signals, or rapid growth?
- What action should a public health reviewer take next?

Important limitation:
- The synthesis layer is heuristic and non-validated.
- SNP support is currently based on precomputed sequence-cluster assignments, not a fully validated SNP alignment pipeline.
- Epidemiological support now includes structured exposure/contact/location evidence where available, but completeness still varies by operational data quality.
- Cluster-risk scores need calibration before real-world use.
- Token-based RBAC is available for API routes. Set `TB_AUTH_REQUIRED=1` and
  configure `TB_AUTH_TOKENS` as `token=role` or `token=subject|role,role`.
  This is not a full NHS identity/access-governance system, but synthesis,
  investigation, ingest, job, and sign-off routes are protected by viewer,
  analyst, operator, and admin role checks.

Calibration and threshold tuning
--------------------------------

The platform now supports a repeatable reviewer-agreement tuning loop.

Core calibration endpoints:

- GET /analytics/case-pair-calibration
- GET /analytics/case-pair-calibration/sweep

Use `/analytics/case-pair-calibration` to inspect agreement for one profile and one threshold set.
Use `/analytics/case-pair-calibration/sweep` to evaluate multiple threshold and weight combinations and return top-ranked candidates by binary Cohen kappa and coverage.

Example sweep query:

```
/analytics/case-pair-calibration/sweep?snp_strong_threshold_options=4,5,6&snp_moderate_threshold_options=10,12,14&temporal_window_options=30,45,60&strong_epi_weight_options=2,3,4&contradiction_weight_options=-2,-3,-4
```

Operational rationale for key defaults:

- `TB_CLUSTER_ALERT_MIN_CASES` (default 5): avoids over-alerting on very small groups where unstable denominators inflate apparent growth.
- `TB_OUTBREAKER_ITER`, `TB_OUTBREAKER_BURNIN`, `TB_OUTBREAKER_THIN`: increase when convergence diagnostics are weak; defaults are set for practical runtime but should be reviewed for larger investigations.
- `TB_OUTBREAKER_SI_MEAN`, `TB_OUTBREAKER_SI_SD`, `TB_OUTBREAKER_SI_MAX`: set serial-interval priors to your programme context and keep assumptions documented in MDT notes.

Decision framework:

- Prefer candidate profiles with higher binary kappa only when reviewed-pair coverage remains adequate.
- Keep pairwise SNP/QC/epi contradiction flags as hard review prompts even when model agreement improves.
- Record chosen profile version and threshold set in governance documentation before operational rollout.

Ingest example bundle (for user onboarding)
-------------------------------------------

To help users augment data into an ingest-ready shape, the project includes a generator that
creates realistic synthetic templates using public TB incidence data where available:

- Script: `scripts/generate_ingest_example_bundle.py`
- Output folder: `examples/ingest_bundle/`
- Guide: `examples/ingest_bundle/README.md`

Run from project root:

```bash
python scripts/generate_ingest_example_bundle.py --cases 40 --output-dir examples/ingest_bundle
```

The bundle includes CSV files for core tables, FASTA sequence examples, a manifest, and a SQL
loader template so users can validate formatting before loading real programme data.

Pilot-ready internal demonstrator milestone
-------------------------------------------

The near-term implementation target is documented in
`docs/governance/PILOT_READY_INTERNAL_DEMONSTRATOR.md`. It covers authenticated
token-based RBAC deployment, validated ingest, operational readiness checks,
structured epidemiology linkage, reproducible reports, tests, CI, and governance
caveats for an internal pilot.
