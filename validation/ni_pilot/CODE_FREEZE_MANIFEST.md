# NI Pilot Code Freeze Manifest

Status: Draft-only, not a formal freeze
Generated: 2026-05-27

## Repository Snapshot

- Branch: `main`
- Commit: `e66bf6c6130b75a39a0829b36d56a252953b6406`
- Short commit: `e66bf6c`
- Application version: `0.6.0`
- Python target: `>=3.11,<3.12`

## Freeze Decision

The code is not formally frozen yet.

Blocking reasons:

- The worktree is dirty.
- `docs/SCIENTIFIC_CALIBRATION_FIXES.md` is deleted in the working tree.
- New NI pilot validation files have been added but are not committed.
- A representative pseudonymised NI pilot dataset has not been supplied or declared.

## Required Freeze Actions

1. Review and resolve the deleted `docs/SCIENTIFIC_CALIBRATION_FIXES.md` file.
2. Commit the validation protocol, validation runner, and report template.
3. Create a release branch or tag for the NI pilot validation candidate.
4. Re-run:

```powershell
.\.venv\Scripts\python.exe scripts\run_ni_pilot_validation.py `
  --dataset path\to\representative_ni_bundle `
  --confirm-representative-ni-dataset `
  --copy-latest
```

5. Confirm the generated report status is `awaiting-signature`.
6. Complete reviewer sign-off before using the report as a signed validation record.
