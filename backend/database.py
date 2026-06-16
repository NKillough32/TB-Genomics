from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from backend.settings import load_settings

DATABASE_URL = load_settings().database_url
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

def init_db():
    """Apply Alembic migrations at startup unless explicitly disabled."""
    if not load_settings().auto_migrate:
        return

    project_root = Path(__file__).resolve().parent.parent
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")

