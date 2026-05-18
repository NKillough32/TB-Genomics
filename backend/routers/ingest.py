
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile

from backend.synthetic_seed import seed_synthetic_dataset
from scripts.load_ingest_bundle import load_bundle
from scripts.validate_ingest_files import validate_bundle

router = APIRouter(prefix="/ingest", tags=["ingest"])
os.makedirs("uploads", exist_ok=True)


def _require_ingest_api_key(x_api_key: str = Header(default="")):
    """Require X-API-Key when TB_INGEST_API_KEY is configured.

    This keeps local development simple while allowing production hardening
    via environment configuration.
    """
    configured_key = os.getenv("TB_INGEST_API_KEY", "").strip()
    if not configured_key:
        return
    if x_api_key != configured_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

@router.post("/file")
def ingest_file(
    file: UploadFile = File(...),
    load_to_database: bool = Query(
        True,
        description="For ZIP ingest bundles, validate and load into the database after upload.",
    ),
    dry_run: bool = Query(False, description="Validate and parse without writing database rows."),
    strict_analysis: bool = Query(
        True,
        description="Fail if analysis-critical optional files are missing or incomplete.",
    ),
    _auth: None = Depends(_require_ingest_api_key),
):
    # Path.name avoids directory traversal from crafted upload filenames.
    safe_name = Path(file.filename).name
    path = Path("uploads") / safe_name
    with open(path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    if zipfile.is_zipfile(path):
        with tempfile.TemporaryDirectory(prefix="tb-ingest-") as tmp:
            bundle_dir = Path(tmp) / "bundle"
            bundle_dir.mkdir()
            with zipfile.ZipFile(path) as archive:
                _safe_extract_zip(archive, bundle_dir)
            root = _resolve_bundle_root(bundle_dir)
            validation = validate_bundle(root, strict_analysis=strict_analysis)
            if validation["summary"]["fail"]:
                return {
                    "status": "validation_failed",
                    "filename": safe_name,
                    "database_loaded": False,
                    "validation": validation,
                }
            if not load_to_database:
                return {
                    "status": "validated",
                    "filename": safe_name,
                    "database_loaded": False,
                    "validation": validation,
                }
            results = load_bundle(root, dry_run=dry_run)
            load_summary = {
                result.table: {
                    "rows_processed": result.inserted,
                    "errors": result.errors,
                }
                for result in results
            }
            load_errors = sum(len(result.errors) for result in results)
            return {
                "status": "loaded" if not dry_run and load_errors == 0 else "dry_run" if dry_run else "load_failed",
                "filename": safe_name,
                "database_loaded": bool(not dry_run and load_errors == 0),
                "validation": validation,
                "load": load_summary,
            }

    return {
        "status": "uploaded_only",
        "filename": safe_name,
        "database_loaded": False,
        "next_steps": [
            "Prepare a complete ingest bundle if needed.",
            "Run scripts/validate_ingest_files.py before loading.",
            "Run scripts/load_ingest_bundle.py to insert or update database rows.",
        ],
        "message": (
            "File was saved to uploads/ only. This endpoint does not validate "
            "or load data into the database."
        ),
    }


def _safe_extract_zip(archive: zipfile.ZipFile, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    for member in archive.infolist():
        destination = (target_dir / member.filename).resolve()
        try:
            destination.relative_to(target_root)
        except ValueError:
            raise HTTPException(status_code=400, detail="ZIP contains an unsafe path")
    archive.extractall(target_dir)


def _resolve_bundle_root(extracted_dir: Path) -> Path:
    if (extracted_dir / "cases.csv").exists():
        return extracted_dir
    candidates = [p for p in extracted_dir.iterdir() if p.is_dir() and (p / "cases.csv").exists()]
    if len(candidates) == 1:
        return candidates[0]
    raise HTTPException(
        status_code=422,
        detail="ZIP must contain cases.csv at the root or inside one top-level bundle directory",
    )


@router.post("/seed-synthetic")
def seed_synthetic(
    case_count: int = Query(250, ge=10, le=5000),
    reset: bool = Query(False),
    seed: int = Query(42),
    countries: str = Query(
        None,
        description=(
            "Comma-separated ISO3 codes or country names to restrict seeding. "
            "Example: GBR,IRL  Leave blank for all countries."
        ),
    ),
    _auth: None = Depends(_require_ingest_api_key),
):
    if os.getenv("TB_ENABLE_SYNTHETIC_SEEDING", "0") != "1":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "synthetic_seeding_disabled",
                "message": "Synthetic data seeding is disabled by policy.",
                "enable_env": "TB_ENABLE_SYNTHETIC_SEEDING=1",
            },
        )

    country_list = [c.strip() for c in countries.split(",") if c.strip()] if countries else None
    return seed_synthetic_dataset(
        case_count=case_count, reset=reset, seed=seed, countries=country_list
    )

