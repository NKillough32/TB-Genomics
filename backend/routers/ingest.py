
from fastapi import APIRouter, UploadFile, File, Query
import shutil, os

from backend.synthetic_seed import seed_synthetic_dataset

router = APIRouter(prefix="/ingest", tags=["ingest"])
os.makedirs("uploads", exist_ok=True)

@router.post("/file")
def ingest_file(file: UploadFile = File(...)):
    path = f"uploads/{file.filename}"
    with open(path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    return {"status": "uploaded", "filename": file.filename}


@router.post("/seed-synthetic")
def seed_synthetic(
    case_count: int = Query(250, ge=10, le=5000),
    reset: bool = Query(False),
    seed: int = Query(42),
):
    return seed_synthetic_dataset(case_count=case_count, reset=reset, seed=seed)
