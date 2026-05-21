# Posit Connect deployment preparation

This project can keep its current Windows workflow while being prepared for Posit Connect.
The Windows batch files still use the repository-local `exports/`, `uploads/`, and `logs/`
folders by default.

## Recommended deployment shape

Deploy the interactive platform as a Python FastAPI app:

```powershell
rsconnect deploy fastapi `
  --entrypoint backend.app:app `
  --title "TB Genomic Surveillance" `
  .
```

Use Quarto as a reporting layer for generated outputs, not as a wrapper around the whole API.
A Quarto report can read JSON/CSV/PNG artifacts from the configured exports directory and can
be published separately once a `quarto/` project is added.

## Posit Connect requirements

Ask the Connect administrator to confirm:

- Python 3.11 is available to Connect.
- Python support is enabled.
- PostgreSQL is reachable from the Connect execution environment.
- R is available if real outbreaker2 runs will execute inside Connect.
- R packages `outbreaker2`, `ape`, and `jsonlite` are installed for the R runtime used by Connect.
- Quarto is installed if Quarto reports will be rendered or published from Connect.

## Environment variables

Set these in Posit Connect for deployed content:

```text
DATABASE_URL=postgresql://...
TB_AUTH_REQUIRED=1
TB_AUTH_TOKENS=<token>=admin
TB_AUTO_MIGRATE=1
TB_CORS_ORIGINS=<connect-content-origin>
TB_ALLOW_MOCK_OUTBREAKER=0
TB_OUTBREAKER_TIMEOUT_SEC=1200
TB_OUTBREAKER_ITER=50000
TB_OUTBREAKER_BURNIN=10000
TB_OUTBREAKER_THIN=10
```

Runtime directories are optional. Leave them unset on Windows to preserve the current layout.
Set them on Connect if runtime state should live outside the deployed bundle:

```text
TB_EXPORTS_DIR=/var/lib/tb-genomics/exports
TB_UPLOADS_DIR=/var/lib/tb-genomics/uploads
TB_LOGS_DIR=/var/lib/tb-genomics/logs
```

## Readiness check

Run this before publishing, and again inside the target execution environment if possible:

```powershell
python scripts/check_posit_readiness.py
```

The checker validates Python package availability, database connectivity, writable runtime
directories, R/outbreaker2 availability, and optional Quarto availability.

## Current Windows compatibility

The default behaviour remains:

- `exports/` for generated analysis artifacts.
- `uploads/` for uploaded ingest bundles and sequence inputs.
- `logs/` for job logs.
- `backend.app:app` for local `uvicorn` startup.

No existing Windows startup command needs to change unless you intentionally set the new
runtime directory environment variables.
