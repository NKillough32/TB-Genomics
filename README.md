
TB Genomic Surveillance Platform
===============================

This project helps TB teams turn sequencing-linked case data into clear operational insight.

It supports day-to-day surveillance by:
- identifying potential genomic clusters
- tracking programme performance (coverage, QC, turnaround)
- prioritizing where follow-up is most urgent
- producing repeatable investigation reports for review

Current prototype capabilities include:
- FastAPI backend APIs for cases, ingest, KPIs, jobs, and reporting
- Button-driven workflow for running and tracking analysis jobs
- Web interface for operational use
- PostgreSQL data model for surveillance and WGS reporting
- Outbreaker2 integration for analyst-led outbreak analysis
- Governance/setup documentation for secure deployment and integration

Plain-language project overview
------------------------------

For a simple explanation of what this project does and how it supports TB programme operations,
see: docs/PROJECT_EXPLAINED_SIMPLE.md

External dependencies (not bundled):
- PostgreSQL
- Python >= 3.10
- R >= 4.1 with outbreaker2

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
- Double-click `Setup-And-Start-Platform.bat` for first-time setup (creates `.venv`, installs dependencies, then starts backend + GUI).
- Double-click `Start-Backend-Demo.bat` for backend in demo mode (requires typing DEMO confirmation).
- Double-click `Start-Platform-Demo.bat` for backend + GUI in demo mode (requires typing DEMO confirmation).

Notes:
- These launchers expect `.venv` to already exist with dependencies installed.
- If `DATABASE_URL` is not set, the scripts default to:
	`postgresql://tb:tb@localhost/tb_surveillance`
- Demo launchers set `TB_ENABLE_SYNTHETIC_SEEDING=1` and `TB_ALLOW_NON_OPERATIONAL_ACTIONS=1` for that session only.

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

Expected status values:
- `operational_safe: true` means actions are allowed.
- `operational_safe: false` means synthetic/demo signals were detected and sensitive actions are blocked.

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


Publication-friendly outbreak report
------------------------------------

The outbreak report is available in two formats:

- GET /cases/outbreak-report for the existing PDF report.
- GET /cases/outbreak-report.html for a browser-friendly HTML report that is saved to exports/outbreaker_investigation_report.html.

Use the HTML report for online publication workflows after local information-governance review. The HTML output embeds outbreak graphics and uses responsive tables to reduce PDF-only wrapping and page-break formatting issues.

TB surveillance KPI reporting
-----------------------------

New endpoint:

- GET /cases/surveillance-kpis?weeks=12

This returns programme-level metrics including sequencing coverage and QC indicators.

Schema additions for WGS reporting:

- sequencing_runs
- sample_qc_metrics
- analysis_provenance

If your database was created before these additions, apply the updated db/schema.sql.
The endpoint remains backward-compatible and returns a warning until QC tables exist.

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
