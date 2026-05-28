# NI Pilot Validation Package

This folder contains the formal validation package for an internal Northern Ireland TB genomics pilot.

Files:

- `NI_PILOT_VALIDATION_PROTOCOL.md`: formal validation protocol.
- `VALIDATION_REPORT_TEMPLATE.md`: human sign-off report template.
- `CODE_FREEZE_MANIFEST.md`: current freeze status and required freeze actions.
- `runs/`: generated validation evidence from `scripts/run_ni_pilot_validation.py`.

Current status:

- Draft evidence has been generated against `examples/ingest_bundle`.
- That bundle passed ingest validation, but it is not declared as the representative pseudonymised NI pilot dataset.
- The code freeze is not clean, so the current validation report is blocked and must not be treated as signed pilot validation.

Formal run command:

```powershell
.\.venv\Scripts\python.exe scripts\run_ni_pilot_validation.py `
  --dataset path\to\representative_ni_bundle `
  --confirm-representative-ni-dataset `
  --copy-latest
```

Formal pass requires a clean code freeze, a declared representative pseudonymised NI dataset, passing validation checks, and completed signatures.
