# NI Pilot Validation Report

- Status: `blocked`
- Run ID: `20260527T111813Z`
- Generated at: `2026-05-27T11:18:19.411393+00:00`
- Branch: `main`
- Commit: `e66bf6c6130b75a39a0829b36d56a252953b6406`
- Dataset: `C:\Users\Nicho\Desktop\TB-Genomics-main\examples\ingest_bundle`

## Decision

Formal NI pilot validation is blocked until failed checks are remediated and the report is signed.

## Checks

| Check | Status | Evidence |
| --- | --- | --- |
| VP-001 Code freeze | draft-only | Commit e66bf6c on main. |
| VP-002 NI ingest bundle validation | pass | validation\ni_pilot\runs\20260527T111813Z\vp002_ingest_validation.log |
| VP-002A Representative NI dataset declaration | fail | Dataset has not been declared representative pseudonymised NI pilot data. |
| VP-003 WGS contract regression | pass | validation\ni_pilot\runs\20260527T111813Z\vp003_wgs_regression.log |
| VP-004 Automated test suite | pass | validation\ni_pilot\runs\20260527T111813Z\vp004_pytest.log |
| VP-005 Operational safety flags | pass | {"TB_ALLOW_NON_OPERATIONAL_ACTIONS": null, "TB_AUTH_REQUIRED": null, "TB_AUTH_TOKENS_configured": false, "TB_ENABLE_SYNTHETIC_SEEDING": null} |

## Code Freeze

- Dirty worktree: `True`
- Dirty files: `D docs/SCIENTIFIC_CALIBRATION_FIXES.md, ?? scripts/run_ni_pilot_validation.py, ?? validation/ni_pilot/`

## Sign-Off

This report is generated evidence. It is not signed until the roles below are completed.

| Role | Name | Decision | Signature | Date |
| --- | --- | --- | --- | --- |
| Technical validation lead |  |  |  |  |
| Public health / epidemiology reviewer |  |  |  |  |
| Information governance reviewer |  |  |  |  |
| Pilot owner / service lead |  |  |  |  |
