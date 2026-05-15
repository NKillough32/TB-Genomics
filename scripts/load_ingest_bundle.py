#!/usr/bin/env python3
"""Load a prepared ingest bundle directory into the TB platform database.

This is the server-side loader that runs on the Azure VM (or any host with
direct database access). It is idempotent: current-state rows are upserted
so corrected records replace earlier values. Provenance rows remain
append-only for audit/history.

Expected bundle layout (identical to examples/ingest_bundle/):

    bundle/
        cases.csv                   REQUIRED
        sequencing_runs.csv         optional, recommended
        tb_interpretation.csv       optional, recommended
        sample_qc_metrics.csv       optional
        analysis_provenance.csv     optional
        dna.fasta                   optional (consensus sequences)

Run validate_ingest_files.py first to catch shape errors before attempting
a database load.

Usage (from project root, venv active):

    python scripts/load_ingest_bundle.py --dir path/to/bundle
    python scripts/load_ingest_bundle.py --dir path/to/bundle --dry-run
    python scripts/load_ingest_bundle.py --dir path/to/bundle --reset

Flags:
    --dir DIR       Bundle directory containing CSV/FASTA files.
    --dry-run       Parse and validate rows without writing to the database.
    --reset         TRUNCATE all data tables before loading.  Requires
                    explicit --confirm-reset to prevent accidental data loss.
    --confirm-reset Required alongside --reset to execute the truncation.
    --log-level     DEBUG / INFO / WARNING (default: INFO).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

# ---------------------------------------------------------------------------
# DB connection — reuses the application's SessionLocal factory.
# ---------------------------------------------------------------------------
# Add project root to sys.path so backend imports work when this script is
# executed directly from the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402
from backend.database import SessionLocal  # noqa: E402

logger = logging.getLogger("load_ingest_bundle")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _coerce_bool(value: str) -> Optional[bool]:
    """Return Python bool from common string representations, or None if blank."""
    v = value.strip().lower()
    if v in ("", "null", "none"):
        return None
    if v in ("true", "1", "yes", "t"):
        return True
    if v in ("false", "0", "no", "f"):
        return False
    raise ValueError(f"Cannot coerce '{value}' to boolean")


def _coerce_float(value: str) -> Optional[float]:
    v = value.strip()
    if not v or v.lower() in ("null", "none", "na", "n/a"):
        return None
    return float(v)


def _coerce_json(value: str) -> Optional[str]:
    """Return JSON string, normalised; None if blank."""
    v = value.strip()
    if not v or v.lower() in ("null", "none"):
        return None
    # Round-trip through json.loads to validate and normalise
    try:
        return json.dumps(json.loads(v))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc


def _coerce_date(value: str) -> Optional[str]:
    v = value.strip()
    if not v or v.lower() in ("null", "none", ""):
        return None
    # Accept ISO dates and common variants
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date '{v}'")


def _load_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return fieldnames, rows


def _parse_fasta(path: Path) -> Iterator[Tuple[str, str]]:
    """Yield (header, sequence) pairs from a FASTA file."""
    current_header: Optional[str] = None
    current_bases: List[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip()
            if line.startswith(">"):
                if current_header is not None:
                    yield current_header, "".join(current_bases)
                current_header = line[1:].split()[0]  # take first token
                current_bases = []
            else:
                current_bases.append(line.upper())
    if current_header is not None:
        yield current_header, "".join(current_bases)


# ---------------------------------------------------------------------------
# Loaders — one function per table
# ---------------------------------------------------------------------------

class IngestResult:
    def __init__(self, table: str) -> None:
        self.table = table
        self.inserted = 0
        self.skipped = 0
        self.errors: List[str] = []

    def __str__(self) -> str:
        parts = [f"{self.table}: {self.inserted} rows processed"]
        if self.errors:
            parts.append(f"  ERRORS ({len(self.errors)}): {self.errors[:3]}")
        return "\n".join(parts)


def _load_cases(db: Any, rows: List[Dict[str, str]], dry_run: bool) -> IngestResult:
    result = IngestResult("cases")
    for i, row in enumerate(rows, start=2):
        try:
            params = {
                "pseudonymised_case_id": row["pseudonymised_case_id"].strip(),
                "local_lab_sample_id": row.get("local_lab_sample_id", "").strip() or None,
                "specimen_date": _coerce_date(row.get("specimen_date", "")),
                "geographic_region": row.get("geographic_region", "").strip() or None,
                "case_status": row.get("case_status", "").strip() or None,
            }
            if not dry_run:
                db.execute(
                    text(
                        "INSERT INTO cases "
                        "(pseudonymised_case_id, local_lab_sample_id, specimen_date, "
                        "geographic_region, case_status) "
                        "VALUES (:pseudonymised_case_id, :local_lab_sample_id, :specimen_date, "
                        ":geographic_region, :case_status) "
                        "ON CONFLICT (pseudonymised_case_id) DO UPDATE SET "
                        "local_lab_sample_id = EXCLUDED.local_lab_sample_id, "
                        "specimen_date = EXCLUDED.specimen_date, "
                        "geographic_region = EXCLUDED.geographic_region, "
                        "case_status = EXCLUDED.case_status"
                    ),
                    params,
                )
            result.inserted += 1
        except Exception as exc:
            result.errors.append(f"row {i}: {exc}")
            logger.debug("cases row %d error: %s", i, exc)
    return result


def _load_sequencing_runs(db: Any, rows: List[Dict[str, str]], dry_run: bool) -> IngestResult:
    result = IngestResult("sequencing_runs")
    for i, row in enumerate(rows, start=2):
        try:
            params = {
                "run_id": row["run_id"].strip(),
                "platform": row.get("platform", "").strip() or None,
                "instrument_name": row.get("instrument_name", "").strip() or None,
                "pipeline_version": row.get("pipeline_version", "").strip() or None,
                "reference_genome": row.get("reference_genome", "").strip() or None,
                "started_at": row.get("started_at", "").strip() or None,
                "completed_at": row.get("completed_at", "").strip() or None,
            }
            if not dry_run:
                db.execute(
                    text(
                        "INSERT INTO sequencing_runs "
                        "(run_id, platform, instrument_name, pipeline_version, "
                        "reference_genome, started_at, completed_at) "
                        "VALUES (:run_id, :platform, :instrument_name, :pipeline_version, "
                        ":reference_genome, :started_at, :completed_at) "
                        "ON CONFLICT (run_id) DO UPDATE SET "
                        "platform = EXCLUDED.platform, "
                        "instrument_name = EXCLUDED.instrument_name, "
                        "pipeline_version = EXCLUDED.pipeline_version, "
                        "reference_genome = EXCLUDED.reference_genome, "
                        "started_at = EXCLUDED.started_at, "
                        "completed_at = EXCLUDED.completed_at"
                    ),
                    params,
                )
            result.inserted += 1
        except Exception as exc:
            result.errors.append(f"row {i}: {exc}")
    return result


def _load_tb_interpretation(db: Any, rows: List[Dict[str, str]], dry_run: bool) -> IngestResult:
    result = IngestResult("tb_interpretation")
    for i, row in enumerate(rows, start=2):
        try:
            params = {
                "sample_id": row["sample_id"].strip(),
                "species_confirmation": row.get("species_confirmation", "").strip() or None,
                "lineage": row.get("lineage", "").strip() or None,
                "sublineage": row.get("sublineage", "").strip() or None,
                "resistance_mutations": _coerce_json(row.get("resistance_mutations", "")),
                "predicted_drug_resistance": _coerce_json(row.get("predicted_drug_resistance", "")),
                "confidence_score": _coerce_float(row.get("confidence_score", "")),
                "interpretation_summary": row.get("interpretation_summary", "").strip() or None,
            }
            if not dry_run:
                db.execute(
                    text(
                        "INSERT INTO tb_interpretation "
                        "(sample_id, species_confirmation, lineage, sublineage, "
                        "resistance_mutations, predicted_drug_resistance, "
                        "confidence_score, interpretation_summary) "
                        "VALUES (:sample_id, :species_confirmation, :lineage, :sublineage, "
                        "CAST(:resistance_mutations AS jsonb), "
                        "CAST(:predicted_drug_resistance AS jsonb), "
                        ":confidence_score, :interpretation_summary) "
                        "ON CONFLICT (sample_id) DO UPDATE SET "
                        "species_confirmation = EXCLUDED.species_confirmation, "
                        "lineage = EXCLUDED.lineage, "
                        "sublineage = EXCLUDED.sublineage, "
                        "resistance_mutations = EXCLUDED.resistance_mutations, "
                        "predicted_drug_resistance = EXCLUDED.predicted_drug_resistance, "
                        "confidence_score = EXCLUDED.confidence_score, "
                        "interpretation_summary = EXCLUDED.interpretation_summary"
                    ),
                    params,
                )
            result.inserted += 1
        except Exception as exc:
            result.errors.append(f"row {i}: {exc}")
    return result


def _load_sample_qc_metrics(db: Any, rows: List[Dict[str, str]], dry_run: bool) -> IngestResult:
    result = IngestResult("sample_qc_metrics")
    for i, row in enumerate(rows, start=2):
        try:
            params = {
                "sample_id": row["sample_id"].strip(),
                "run_id": row.get("run_id", "").strip() or None,
                "mean_depth": _coerce_float(row.get("mean_depth", "")),
                "coverage_breadth": _coerce_float(row.get("coverage_breadth", "")),
                "ambiguous_base_percent": _coerce_float(row.get("ambiguous_base_percent", "")),
                "contamination_flag": _coerce_bool(row.get("contamination_flag", "false")),
                "qc_status": row.get("qc_status", "").strip().lower() or None,
                "qc_failure_reason": row.get("qc_failure_reason", "").strip() or None,
                "reported_at": row.get("reported_at", "").strip() or None,
            }
            if not dry_run:
                db.execute(
                    text(
                        "INSERT INTO sample_qc_metrics "
                        "(sample_id, run_id, mean_depth, coverage_breadth, "
                        "ambiguous_base_percent, contamination_flag, qc_status, "
                        "qc_failure_reason, reported_at) "
                        "VALUES (:sample_id, :run_id, :mean_depth, :coverage_breadth, "
                        ":ambiguous_base_percent, :contamination_flag, :qc_status, "
                        ":qc_failure_reason, :reported_at) "
                        "ON CONFLICT (sample_id) DO UPDATE SET "
                        "run_id = EXCLUDED.run_id, "
                        "mean_depth = EXCLUDED.mean_depth, "
                        "coverage_breadth = EXCLUDED.coverage_breadth, "
                        "ambiguous_base_percent = EXCLUDED.ambiguous_base_percent, "
                        "contamination_flag = EXCLUDED.contamination_flag, "
                        "qc_status = EXCLUDED.qc_status, "
                        "qc_failure_reason = EXCLUDED.qc_failure_reason, "
                        "reported_at = EXCLUDED.reported_at"
                    ),
                    params,
                )
            result.inserted += 1
        except Exception as exc:
            result.errors.append(f"row {i}: {exc}")
    return result


def _load_analysis_provenance(db: Any, rows: List[Dict[str, str]], dry_run: bool) -> IngestResult:
    result = IngestResult("analysis_provenance")
    for i, row in enumerate(rows, start=2):
        try:
            params = {
                "sample_id": row.get("sample_id", "").strip() or None,
                "pipeline_name": row.get("pipeline_name", "").strip() or None,
                "pipeline_version": row.get("pipeline_version", "").strip() or None,
                "reference_genome": row.get("reference_genome", "").strip() or None,
                "software_versions": _coerce_json(row.get("software_versions", "")),
                "parameters": _coerce_json(row.get("parameters", "")),
                "generated_at": row.get("generated_at", "").strip() or None,
            }
            if not dry_run:
                db.execute(
                    text(
                        "INSERT INTO analysis_provenance "
                        "(sample_id, pipeline_name, pipeline_version, reference_genome, "
                        "software_versions, parameters, generated_at) "
                        "VALUES (:sample_id, :pipeline_name, :pipeline_version, "
                        ":reference_genome, CAST(:software_versions AS jsonb), "
                        "CAST(:parameters AS jsonb), :generated_at)"
                    ),
                    params,
                )
            result.inserted += 1
        except Exception as exc:
            result.errors.append(f"row {i}: {exc}")
    return result


def _load_fasta(db: Any, fasta_path: Path, dry_run: bool) -> IngestResult:
    result = IngestResult("consensus_sequences (dna.fasta)")
    for seq_id, sequence in _parse_fasta(fasta_path):
        if not UUID_RE.match(seq_id):
            result.errors.append(f"FASTA header '{seq_id}' is not a UUID — skipped")
            continue
        try:
            if not dry_run:
                db.execute(
                    text(
                        "INSERT INTO consensus_sequences (sample_id, sequence, length) "
                        "VALUES (:sample_id, :sequence, :length) "
                        "ON CONFLICT (sample_id) DO UPDATE SET "
                        "sequence = EXCLUDED.sequence, "
                        "length = EXCLUDED.length"
                    ),
                    {"sample_id": seq_id, "sequence": sequence, "length": len(sequence)},
                )
            result.inserted += 1
        except Exception as exc:
            result.errors.append(f"sequence '{seq_id}': {exc}")
    return result


def _record_audit(db: Any, bundle_dir: Path, results: List[IngestResult]) -> None:
    summary = {
        "bundle_dir": str(bundle_dir),
        "tables": {r.table: {"inserted": r.inserted, "errors": len(r.errors)} for r in results},
    }
    try:
        db.execute(
            text(
                "INSERT INTO audit_log (action, user_id, details, timestamp) "
                "VALUES ('bundle_ingest', 'system', CAST(:details AS jsonb), NOW())"
            ),
            {"details": json.dumps(summary)},
        )
    except Exception:
        db.rollback()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def load_bundle(
    bundle_dir: Path,
    dry_run: bool = False,
    reset: bool = False,
    confirm_reset: bool = False,
) -> List[IngestResult]:
    if reset and not confirm_reset:
        print(
            "ERROR: --reset requires --confirm-reset to prevent accidental data loss.\n"
            "       Pass both flags together: --reset --confirm-reset"
        )
        sys.exit(1)

    results: List[IngestResult] = []
    db = SessionLocal()

    try:
        if reset and not dry_run:
            logger.warning("Truncating all data tables (--reset --confirm-reset specified)")
            db.execute(
                text(
                    "TRUNCATE TABLE case_clusters, tb_interpretation, clusters, "
                    "sample_qc_metrics, consensus_sequences, analysis_provenance, "
                    "sequencing_runs, audit_log, cases RESTART IDENTITY CASCADE"
                )
            )
            db.commit()
            logger.info("Truncation complete")

        # ── cases.csv (required) ───────────────────────────────────────────
        cases_path = bundle_dir / "cases.csv"
        if not cases_path.exists():
            print(f"ERROR: cases.csv not found in {bundle_dir}")
            sys.exit(1)
        _, case_rows = _load_csv(cases_path)
        logger.info("Loading cases.csv (%d rows)…", len(case_rows))
        r = _load_cases(db, case_rows, dry_run)
        results.append(r)
        if r.errors:
            logger.warning("%d errors in cases.csv (first: %s)", len(r.errors), r.errors[0])

        # ── sequencing_runs.csv (optional) ────────────────────────────────
        runs_path = bundle_dir / "sequencing_runs.csv"
        if runs_path.exists():
            _, run_rows = _load_csv(runs_path)
            logger.info("Loading sequencing_runs.csv (%d rows)…", len(run_rows))
            results.append(_load_sequencing_runs(db, run_rows, dry_run))
        else:
            logger.info("sequencing_runs.csv not found — skipping")

        # ── tb_interpretation.csv (optional) ─────────────────────────────
        interp_path = bundle_dir / "tb_interpretation.csv"
        if interp_path.exists():
            _, interp_rows = _load_csv(interp_path)
            logger.info("Loading tb_interpretation.csv (%d rows)…", len(interp_rows))
            results.append(_load_tb_interpretation(db, interp_rows, dry_run))
        else:
            logger.info("tb_interpretation.csv not found — skipping")

        # ── sample_qc_metrics.csv (optional) ─────────────────────────────
        qc_path = bundle_dir / "sample_qc_metrics.csv"
        if qc_path.exists():
            _, qc_rows = _load_csv(qc_path)
            logger.info("Loading sample_qc_metrics.csv (%d rows)…", len(qc_rows))
            results.append(_load_sample_qc_metrics(db, qc_rows, dry_run))
        else:
            logger.info("sample_qc_metrics.csv not found — skipping")

        # ── analysis_provenance.csv (optional) ───────────────────────────
        prov_path = bundle_dir / "analysis_provenance.csv"
        if prov_path.exists():
            _, prov_rows = _load_csv(prov_path)
            logger.info("Loading analysis_provenance.csv (%d rows)…", len(prov_rows))
            results.append(_load_analysis_provenance(db, prov_rows, dry_run))
        else:
            logger.info("analysis_provenance.csv not found — skipping")

        # ── dna.fasta (optional) ─────────────────────────────────────────
        fasta_path = bundle_dir / "dna.fasta"
        if fasta_path.exists():
            logger.info("Loading dna.fasta…")
            results.append(_load_fasta(db, fasta_path, dry_run))
        else:
            logger.info("dna.fasta not found — skipping")

        if not dry_run:
            _record_audit(db, bundle_dir, results)
            db.commit()
            logger.info("Committed to database")
        else:
            logger.info("DRY RUN — no changes written")

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load a prepared ingest bundle into the TB platform database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dir",
        required=True,
        help="Path to the bundle directory (must contain at least cases.csv)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse files without writing to the database",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Truncate all data tables before loading (requires --confirm-reset)",
    )
    parser.add_argument(
        "--confirm-reset",
        action="store_true",
        help="Required alongside --reset — prevents accidental truncation",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING"],
        help="Logging verbosity (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    bundle_dir = Path(args.dir).resolve()
    if not bundle_dir.is_dir():
        print(f"ERROR: {bundle_dir} is not a directory")
        sys.exit(1)

    print(f"Bundle directory : {bundle_dir}")
    print(f"Dry run          : {args.dry_run}")
    print(f"Reset tables     : {args.reset}")
    print()

    results = load_bundle(
        bundle_dir,
        dry_run=args.dry_run,
        reset=args.reset,
        confirm_reset=args.confirm_reset,
    )

    total_inserted = 0
    total_errors = 0
    print("\n── Ingest summary ──────────────────────────────────────")
    for r in results:
        status = "OK" if not r.errors else "WARN"
        print(f"  [{status}] {r.table}: {r.inserted} rows processed, {len(r.errors)} errors")
        total_inserted += r.inserted
        total_errors += len(r.errors)
    print(f"\n  Total rows processed : {total_inserted}")
    print(f"  Total errors         : {total_errors}")

    if total_errors:
        print("\nRun with --log-level DEBUG to see full error detail.")
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
