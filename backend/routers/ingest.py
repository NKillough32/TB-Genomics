
from pathlib import Path
import os
import shutil

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile

from backend.synthetic_seed import seed_synthetic_dataset

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
    _auth: None = Depends(_require_ingest_api_key),
):
    # Path.name avoids directory traversal from crafted upload filenames.
    safe_name = Path(file.filename).name
    path = Path("uploads") / safe_name
    with open(path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    return {"status": "uploaded", "filename": safe_name}


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
