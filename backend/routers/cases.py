
from fastapi import APIRouter, Depends
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
