# NI TB Genomic Surveillance Pilot Validation Protocol

Document ID: NI-TB-GEN-VAL-001
Version: 1.0
Status: Draft for pilot execution

## Purpose

This protocol defines the evidence required before the TB Genomic Surveillance Platform can be used as a reproducible internal Northern Ireland pilot on pseudonymised TB programme data.

The protocol validates that the frozen code can ingest, analyse, and report on a representative pseudonymised NI dataset in a controlled, repeatable way. It does not claim clinical validation, diagnostic approval, or automated public-health decision making.

## Scope

In scope:

- Repository code freeze and reproducibility record.
- Pseudonymised NI ingest bundle validation.
- Database/schema readiness checks.
- WGS contract regression validation.
- Backend automated test suite.
- Report-generation and governance-output checks.
- Evidence capture for reviewer sign-off.

Out of scope:

- Direct patient-identifiable data processing.
- Clinical treatment decisions.
- Formal NHS identity-provider integration.
- External production deployment validation.
- Replacement of epidemiological or clinical review.

## Required Inputs

The validation lead must provide a representative pseudonymised NI dataset as an ingest bundle directory containing:

- `cases.csv`
- `tb_interpretation.csv`
- `sequencing_runs.csv`
- `sample_qc_metrics.csv`
- `analysis_provenance.csv`
- `dna.fasta`
- `ingest_manifest.json`

The dataset must contain only pseudonymised identifiers. It must represent the intended pilot data mix, including clustered and non-clustered cases where available, QC pass/fail examples where available, lineage and resistance examples where available, and realistic missingness.

## Code Freeze Requirements

Before pilot validation:

1. The validation branch or release commit must be identified by Git commit SHA.
2. The worktree must be clean unless the validation report explicitly records every uncommitted file.
3. Dependency versions must be captured from:
   - `pyproject.toml`
   - `backend/requirements.txt`
   - `requirements-dev.txt`
4. Validation outputs must be written under `validation/ni_pilot/runs/<run_id>/`.
5. Any post-freeze code change invalidates the validation run and requires re-run.

## Validation Tests

### VP-001 Code Freeze

Acceptance criteria:

- Git commit SHA captured.
- Branch captured.
- Dirty status is empty for a formal pass.
- If dirty status is not empty, the run status is blocked or draft-only.

### VP-002 Ingest Bundle Validation

Acceptance criteria:

- `scripts/validate_ingest_files.py --dir <dataset>` exits with code 0.
- Required files and required columns are present.
- UUID, date, JSON, FASTA-header, and join-integrity checks pass.
- Warnings are reviewed and recorded.

### VP-003 WGS Contract Regression

Acceptance criteria:

- `validation/run_validation.py` exits with code 0.
- Observed reportable outputs match expected outputs for QC, SNP distances, lineage, resistance, and cluster assignment.

### VP-004 Automated Test Suite

Acceptance criteria:

- `python -m pytest` exits with code 0.
- Any skipped tests or xfails are reviewed and documented.

### VP-005 Operational Safety

Acceptance criteria:

- Synthetic/demo controls are not enabled for pilot validation.
- The dataset is pseudonymised.
- The validation report records that outputs are decision-support only.
- Authentication/RBAC settings for the pilot environment are documented.

### VP-006 Report and Audit Evidence

Acceptance criteria:

- Generated report artefacts are recorded.
- Validation outputs include command logs and machine-readable JSON summary.
- Sign-off section is completed by named reviewers after evidence review.

## Pass/Fail Rules

Formal pilot validation passes only when:

- Code freeze is clean.
- Ingest validation passes on the representative pseudonymised NI dataset.
- WGS regression passes.
- Automated tests pass.
- Reviewer sign-off is completed.

The validation is blocked when:

- No representative pseudonymised NI dataset is supplied.
- The worktree is dirty and not explicitly accepted as draft-only.
- Any required validation command fails.
- Any required sign-off is missing.

## Required Sign-Off

The signed validation report must be reviewed and signed by:

- Technical validation lead.
- Public health / epidemiology reviewer.
- Information governance reviewer.
- Pilot owner or service lead.

Electronic signatures may be used if the organisation has an approved process. Otherwise, names, roles, dates, and wet-signature references must be recorded.
