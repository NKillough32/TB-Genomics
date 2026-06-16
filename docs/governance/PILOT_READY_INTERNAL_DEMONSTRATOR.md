# Pilot-ready internal demonstrator

This milestone defines the minimum bar for an internal pilot deployment. It is not a claim of clinical validation or production NHS identity governance.

## Target state

The demonstrator should support a small authenticated internal user group reviewing pseudonymised TB genomics data with reproducible ingest, readiness checks, epidemiology linkage, reports, tests, CI, and documented governance controls.

## Readiness checklist

| Area | Minimum pilot requirement | Current implementation |
| --- | --- | --- |
| Authenticated multi-user deployment | Token-based RBAC enabled with named subjects and roles in `TB_AUTH_TOKENS`; `TB_AUTH_REQUIRED=1` in deployed environments. | Available. Roles: `viewer`, `analyst`, `operator`, `admin`. Not a full user-login or NHS IAM system. |
| Validated ingest workflow | ZIP ingest bundles validate before loading; CLI validator and loader remain available for controlled operations. | Available through `POST /ingest/file`, `scripts/validate_ingest_files.py`, and `scripts/load_ingest_bundle.py`. |
| Operational readiness checks | Dataset safety and completeness checks visible before analysis/reporting. | Available through `GET /cases/data-safety`, `GET /cases/data-readiness`, and the GUI readiness panel. |
| Epidemiology linkage workflows | Structured tables and APIs for contacts, exposures, locations, case-location events, and case-contact links. | Tables, read/create API routes, reference-record UI, and cluster-investigation case evidence entry are available. Bulk review, edit/delete workflows, and imported-field reconciliation remain follow-on items. |
| Reproducible reports | Report generation records data provenance and blocks synthetic/demo data unless explicitly overridden. | Available for outbreak and cluster investigation reports. Outputs remain decision-support only. |
| Test coverage | Automated tests cover auth roles, synthetic-data blocking, ZIP ingest, data readiness, routes, schema, and migration presence. | Covered by the pytest suite. |
| CI/CD | Pull requests and pushes run tests and source compilation. | GitHub Actions CI is present. Deployment automation remains a follow-on item. |
| Governance model | Limitations, access model, data safety gates, audit trail, and non-validation caveats are documented. | Documented in `docs/governance/`. |

## Pilot operating rules

1. Use pseudonymised identifiers only.
2. Enable `TB_AUTH_REQUIRED=1` and configure per-user or per-service token subjects using `token=subject|role,role`.
3. Keep synthetic seeding disabled in operational pilot environments.
4. Confirm `GET /cases/data-safety` reports `operational_safe: true` before public-health report generation.
5. Review `GET /cases/data-readiness` before analysis and record missingness caveats in pilot notes.
6. Treat synthesis scores, cluster-risk scores, and inferred transmission links as heuristic review aids only.
7. Keep all generated reports inside the internal pilot audience unless separately reviewed and approved.
8. Preserve audit logs for ingest, jobs, sign-off, and report actions.

## Remaining gaps before external production use

- Full identity provider integration, session management, and organisational access governance.
- Formal validation and calibration of risk scores, synthesis thresholds, and report interpretation.
- Bulk review, edit/delete workflows, and imported-field reconciliation for structured epidemiology links.
- Deployment automation, backups, monitoring, and incident response procedures.
- Signed-off data retention, DPIA, information governance, and operational SOPs.

