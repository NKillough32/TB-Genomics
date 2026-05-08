
TB GENOMIC SURVEILLANCE PLATFORM – v0.6 (POLISHED UI + AUTOMATION)
=================================================================

This bundle contains a near‑deployment‑ready prototype for the NI TB Genomic Surveillance
platform with:
- Fully‑wired FastAPI backend
- Button‑driven automation (jobs, progress, logs)
- Polished, NHS‑appropriate web GUI
- PostgreSQL schema
- Outbreaker2 integration (analyst‑led)
- Governance notes and test instructions

External dependencies (not bundled):
- PostgreSQL
- Python >= 3.10
- R >= 4.1 with outbreaker2

Quick start:
1) psql < db/schema.sql
2) pip install -r backend/requirements.txt
3) uvicorn backend.app:app --reload
4) cd gui && python -m http.server 8081
5) Open http://localhost:8081

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

Azure VM sequencing integration
-------------------------------

If sequencing output is generated on an Azure VM and needs to feed this platform,
use the connector script and setup guide:

- scripts/azure_vm_ingest_connector.py
- governance/AZURE_VM_INGEST_SETUP.md

The connector supports scheduled uploads and only sends new/changed files.

To enforce ingest authentication, set TB_INGEST_API_KEY on the backend host and configure
TB_API_KEY with the same value in the VM connector environment.

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
