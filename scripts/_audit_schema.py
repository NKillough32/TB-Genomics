import os
import sys

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

sys.path.insert(0, ".")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from backend.database import SessionLocal  # noqa: E402


def _print_db_error(section: str, exc: Exception) -> None:
    print(f"{section}: ERROR {exc}")


def main() -> int:
    """Print a lightweight schema/data audit without crashing if PostgreSQL is down."""
    db = SessionLocal()
    tables = [
        "cases",
        "tb_interpretation",
        "consensus_sequences",
        "sequencing_runs",
        "sample_qc_metrics",
        "analysis_provenance",
        "clusters",
        "case_clusters",
        "exposures",
        "contacts",
        "locations",
        "case_location_events",
        "case_contact_links",
        "audit_log",
    ]

    try:
        print("=== ROW COUNTS ===")
        for table_name in tables:
            try:
                count = db.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar()
                print(f"  {table_name}: {count}")
            except SQLAlchemyError as exc:
                print(f"  {table_name}: ERROR {exc}")

        print("\n=== COLUMNS ===")
        try:
            rows = db.execute(
                text(
                    "SELECT table_name, column_name, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "ORDER BY table_name, ordinal_position"
                )
            ).fetchall()
            for row in rows:
                print(f"  {row[0]:30s}  {row[1]:35s}  {row[2]}")
        except SQLAlchemyError as exc:
            _print_db_error("  columns", exc)
            return 2

        print("\n=== SAMPLE DATA (cases) ===")
        try:
            rows = db.execute(text("SELECT * FROM cases LIMIT 3")).fetchall()
            cols = db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='cases' ORDER BY ordinal_position"
                )
            ).scalars().all()
            print("  COLS:", cols)
            for row in rows:
                print(" ", dict(zip(cols, row)))
        except SQLAlchemyError as exc:
            _print_db_error("  cases sample", exc)

        print("\n=== SAMPLE DATA (sequencing_runs) ===")
        try:
            rows = db.execute(text("SELECT * FROM sequencing_runs LIMIT 3")).fetchall()
            for row in rows:
                print(" ", row)
        except SQLAlchemyError as exc:
            _print_db_error("  sequencing_runs sample", exc)

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

