# TB Genomics Project Explained in Simple Terms

## What this project is

This project is a working system that helps a TB programme turn lab sequencing data into practical public health insight.

In plain terms, it helps answer:

- Which TB cases look genetically linked?
- Are there active clusters that may need investigation?
- How well is sequencing coverage performing across regions?
- What should teams prioritize this week?

## What it does, step by step

1. It receives TB case and sequencing-related data (via direct DB load or the NI ingest pipeline).
2. It stores and organizes that data in a database.
3. It runs analysis jobs including outbreak-style analysis workflows.
4. It calls lineage and drug resistance tools (TBProfiler and Mykrobe) in parallel; both results are compared and discordances are flagged for analyst review.
5. It calculates programme metrics (coverage, QC, turnaround, etc.).
6. It builds a synthesis layer that combines genomic evidence, timing, geography, resistance, and outbreaker outputs into a single investigation summary.
7. It generates summaries, visuals, and a full HTML investigation report.
8. It shows all of this in a web interface and API for operational use.

## The main parts

- **Backend API**: runs the logic, calculations, and report generation.
- **Database schema**: defines how case, cluster, QC, sequencing run, and provenance data are stored.
- **GUI**: gives teams a simple web view of cases, jobs, and outputs.
- **Scripts**: run clustering, outbreaker, lineage/DR, and ingest pipeline utilities.
- **Ingest pipeline**: three-step workflow (prepare → validate → load) for transforming NI programme exports into the platform database.
- **Governance docs**: explain setup and secure integration (for example Azure VM ingestion).

## What users get from it

- Case and cluster summaries
- Surveillance KPIs over a selected time window
- Weekly trends for sequencing and QC performance
- Priority lists (for clusters and likely transmission signals)
- A synthesis view that explains which transmission links are strong, moderate, contradictory, or unsupported
- Automatic flags for things that need attention, such as missing sequence data, cross-region clusters, or resistance signals inside a cluster
- Recommended review actions written in plain language for public health teams
- Lineage and drug resistance results from TBProfiler and Mykrobe with concordance checking
- A generated outbreak investigation HTML report (short and full versions)
- Job status and logs for pipeline runs
- Data provenance and reproducibility fields (reference genome, pipeline version, resistance catalogue, random seed)

## What the synthesis layer means in plain language

The synthesis layer sits between the analytics pages and the investigation pages.

It takes separate outputs, such as:

- SNP or sequence clustering signals
- Outbreaker transmission probabilities
- specimen dates
- geographic region
- lineage and drug resistance information

and turns them into one simple interpretation.

For each possible transmission pair, it can say things like:

- strong support
- moderate support
- genomic-only signal
- model-only signal
- contradictory
- insufficient evidence

It also adds flags such as:

- high posterior but high SNP distance
- low SNP but no known epidemiological or geographic link
- same cluster but wide date spread
- resistance signal inside a cluster
- cross-region cluster
- rapidly growing cluster
- missing sequence or QC data
- low-confidence Outbreaker edge

This is useful because public health teams usually need a short, operational answer rather than several separate technical outputs.

Important: these synthesis scores are heuristic and non-validated. They support triage and review, but they are not a final epidemiological truth.

## Why this is useful for a TB programme

- It turns technical sequencing outputs into decision-ready information.
- It supports faster and more consistent outbreak response.
- It helps monitor sequencing service quality, not just case counts.
- It creates a repeatable reporting process instead of manual collation.
- It gives reviewers one place to see the evidence, the contradictions, and the next recommended action.

## How NI programme data fits in

When NI sequencing export files are available, a three-step ingest pipeline loads them into the platform:

1. **Prepare**: `prepare_ni_data.py` maps NI-format CSVs and FASTA files into the standard bundle format using a JSON column mapping config (`ni_column_map.json`).
2. **Validate**: `validate_ingest_files.py` checks the bundle for schema compliance before loading.
3. **Load**: `load_ingest_bundle.py` inserts the prepared data into the database. All inserts are idempotent — safe to re-run without creating duplicates.

Before using real NI data, the database must contain no synthetic seed events. Check `GET /cases/data-safety` and confirm `operational_safe = true`.

## How Azure VM data fits in

If sequencing files are produced on an Azure VM, the project includes a connector that can push new or changed files into the platform.

This means you can automate data flow from sequencing infrastructure into reporting, instead of manually moving files.

## Important boundaries (what it does not do by itself)

- It does not replace clinical judgment or epidemiology investigation.
- It does not provide direct patient care decisions.
- It depends on data quality and completeness from upstream systems.
- It is a surveillance and operational intelligence tool, not a full LIMS.
- Drug resistance results from TBProfiler and Mykrobe are genomic predictions only — all must be confirmed by phenotypic DST before clinical use.
- The current synthesis layer is not a validated transmission model.
- SNP support is currently derived from precomputed sequence-cluster assignments, not a full validated SNP alignment pipeline.
- Epidemiological support is still a proxy based on timing and geography, so structured contact/exposure fields would improve it.
- Cluster-risk scores need calibration before real-world use.
- There is no RBAC/authentication yet on synthesis outputs or sign-off actions.

## Demo data vs real operational data

The platform supports synthetic data for training and demonstration, but now enforces
a safety split between demo use and operational use.

- Demo mode: synthetic data can be generated and used to show end-to-end workflows.
- Operational mode: sensitive actions are blocked if synthetic/demo signals are present.

What is blocked in non-operational mode:
- Full analysis pipeline
- Outbreak investigation PDF generation
- Bulk export download

How teams should use this in practice:
1. Use demo mode for onboarding, workshops, and testing.
2. Before operational use, disable demo flags and ingest real programme data.
3. Check `/cases/data-safety` and confirm `operational_safe = true` before running public-health actions.

Environment switches:
- `TB_ENABLE_SYNTHETIC_SEEDING=1` enables synthetic seeding.
- `TB_ALLOW_NON_OPERATIONAL_ACTIONS=1` allows analysis/report actions on synthetic data (demo only).

## Short summary in one sentence

This project is a TB genomic surveillance platform that ingests sequencing-linked data, analyses cluster and transmission patterns, tracks programme performance, and produces actionable reports for public health teams.

The new synthesis layer makes the platform feel more like an operational surveillance tool by turning several separate analyses into one plain-language review summary.

## Running it locally on Windows

If you are setting this up on Windows, there is one easy mistake to avoid when loading the database schema.

1. Create and activate a Python virtual environment.
2. Install the Python requirements from `backend/requirements.txt`.
3. Connect to PostgreSQL and create the `tb` user and `tb_surveillance` database if they do not already exist.
4. Exit the PostgreSQL prompt with `\q`.
5. Back in PowerShell, run the schema file using `psql -f .\db\schema.sql`.

The important point is this:

- Commands such as `CREATE USER` and `CREATE DATABASE` run inside the PostgreSQL `psql` prompt.
- Commands such as `psql -f .\db\schema.sql` must be run from PowerShell, not from inside `psql`.

If you see `role "tb" already exists` or `database "tb_surveillance" already exists`, that usually just means setup was already done earlier.
