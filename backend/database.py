from functools import lru_cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from backend.settings import load_settings

Base = declarative_base()


@lru_cache(maxsize=1)
def get_engine():
    return create_engine(load_settings().database_url)


@lru_cache(maxsize=1)
def get_sessionmaker():
    return sessionmaker(bind=get_engine())


def SessionLocal(*args, **kwargs):
    return get_sessionmaker()(*args, **kwargs)


def init_db():
    """Apply Alembic migrations at startup unless explicitly disabled."""
    settings = load_settings()
    if not settings.auto_migrate:
        return

    project_root = Path(__file__).resolve().parent.parent
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
