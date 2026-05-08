
import os

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session
from backend.database import SessionLocal
from backend.models import Case

router = APIRouter(prefix="/cases", tags=["cases"])

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

@router.get("/")
def list_cases(db: Session = Depends(get_db)):
    return db.query(Case).all()


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    total_cases = db.execute(text("SELECT COUNT(*) FROM cases")).scalar() or 0
    clustered_cases = db.execute(text("SELECT COUNT(DISTINCT sample_id) FROM case_clusters")).scalar() or 0
    open_clusters = db.execute(
        text("SELECT COUNT(*) FROM clusters WHERE investigation_status = 'open'")
    ).scalar() or 0

    return {
        "total_cases": int(total_cases),
        "clustered_cases": int(clustered_cases),
        "unclustered_cases": int(total_cases) - int(clustered_cases),
        "open_clusters": int(open_clusters),
    }


@router.get("/outbreaker-status")
def outbreaker_status():
    return {
        "cases_export": os.path.exists("exports/cases.csv"),
        "dna_export": os.path.exists("exports/dna.fasta"),
        "results_rds": os.path.exists("outbreaker2_results.rds"),
    }


@router.get("/audit-trail")
def audit_trail(limit: int = 50, db: Session = Depends(get_db)):
    """Return recent audit log entries for governance and compliance."""
    rows = db.execute(
        text(
            """
            SELECT audit_id, action, user_id, details, timestamp
            FROM audit_log
            ORDER BY timestamp DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings().all()
    
    return {
        "total_entries": len(rows),
        "entries": [
            {
                "id": row["audit_id"],
                "action": row["action"],
                "user": row["user_id"],
                "timestamp": str(row["timestamp"]),
                "details": row["details"],
            }
            for row in rows
        ],
    }
