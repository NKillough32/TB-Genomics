#!/usr/bin/env python3
"""Transform NI-specific TB data exports into the platform's ingest bundle format.

This script reads source CSVs from the NI TB / WGS pipeline (column names and
formats unknown until the system is accessible) and maps them to the standard
ingest bundle shape expected by load_ingest_bundle.py.

Configuration is driven entirely by ni_column_map.json - no Python code changes
are needed for column renames, value remaps, or static defaults.

Usage (from project root, venv active):

    # List columns found in your source file without transforming:
    python scripts/prepare_ni_data.py --list-columns --config scripts/ni_column_map.json

    # Transform all source files to the output bundle directory:
    python scripts/prepare_ni_data.py --config scripts/ni_column_map.json --out prepared_bundle/

    # Preview first 5 rows of the mapped cases output without writing files:
    python scripts/prepare_ni_data.py --config scripts/ni_column_map.json --out prepared_bundle/ --dry-run

After running this script:
    1. python scripts/validate_ingest_files.py --dir prepared_bundle/
    2. python scripts/load_ingest_bundle.py --dir prepared_bundle/
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("prepare_ni_data")

# ---------------------------------------------------------------------------
# Mapping engine
# ---------------------------------------------------------------------------

def _stable_uuid(value: str) -> str:
    """Generate a deterministic UUID v5 from an arbitrary string identifier.

    This ensures that the same lab sample ID always produces the same
    pseudonymised_case_id, so re-runs of this script are idempotent.

    The namespace UUID is arbitrary but fixed - change it only if you need
    to regenerate all IDs from scratch.
    """
    namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # URL namespace
    return str(uuid.uuid5(namespace, str(value).strip()))


def _parse_date(value: str) -> Optional[str]:
    """Normalise a date string to ISO format YYYY-MM-DD."""
    v = value.strip()
    if not v or v.lower() in ("", "null", "none", "n/a", "na"):
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    logger.warning("Could not parse date value '%s' - leaving blank", value)
    return None


def _apply_mapping(
    source_row: Dict[str, str],
    field_map: Dict[str, Any],
    field_name: str,
) -> Optional[str]:
    """Return the mapped value for a single output field."""
    mapping = field_map.get(field_name)
    if mapping is None:
        return None

    mtype = mapping.get("type", "ignore")

    if mtype == "ignore":
        return None

    if mtype == "static":
        return str(mapping["value"])

    source_col = mapping.get("source_column", "")

    # Collect raw value - try exact match, then case-insensitive match
    raw = source_row.get(source_col)
    if raw is None:
        # Case-insensitive column lookup
        lower_map = {k.lower(): v for k, v in source_row.items()}
        raw = lower_map.get(source_col.lower(), "")
    raw = (raw or "").strip()

    if mtype == "column":
        return raw or None

    if mtype == "uuid_from_column":
        if not raw:
            logger.warning("uuid_from_column: source column '%s' is blank", source_col)
            return None
        return _stable_uuid(raw)

    if mtype == "date_column":
        return _parse_date(raw)

    if mtype == "map_values":
        value_map: Dict[str, str] = mapping.get("value_map", {})
        fallback: str = mapping.get("fallback", raw)
        return value_map.get(raw, fallback) or None

    logger.warning("Unknown mapping type '%s' for field '%s'", mtype, field_name)
    return None


def _transform_csv(
    source_path: Path,
    field_map: Dict[str, Any],
    output_columns: List[str],
    dry_run: bool,
    preview_rows: int = 5,
) -> List[Dict[str, Optional[str]]]:
    """Read source CSV, apply column mapping, return list of output rows."""
    if not source_path.exists():
        logger.warning("Source file not found: %s - skipping", source_path)
        return []

    with source_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        source_rows = list(reader)

    logger.info("  Read %d rows from %s", len(source_rows), source_path.name)

    output_rows: List[Dict[str, Optional[str]]] = []
    for source_row in source_rows:
        out = {col: _apply_mapping(source_row, field_map, col) for col in output_columns}
        output_rows.append(out)

    if dry_run and output_rows:
        print(f"\n  Preview of {source_path.name} -> (first {preview_rows} rows):")
        print("  " + ",".join(output_columns))
        for row in output_rows[:preview_rows]:
            vals = [str(row.get(c) or "") for c in output_columns]
            print("  " + ",".join(vals))

    return output_rows


def _write_csv(rows: List[Dict[str, Optional[str]]], columns: List[str], out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c) or "" for c in columns})
    return len(rows)


def _rewrite_fasta_headers(
    source_path: Path,
    out_dir: Path,
    header_map: Dict[str, str],
) -> None:
    """Write dna.fasta with headers remapped to platform case UUIDs when possible."""
    if not source_path.exists():
        logger.info("  FASTA source not found: %s - skipping", source_path)
        return

    dest = out_dir / "dna.fasta"
    dest.parent.mkdir(parents=True, exist_ok=True)
    remapped = 0
    unchanged = 0
    unmapped = 0

    with source_path.open(encoding="utf-8") as src, dest.open("w", encoding="utf-8") as out:
        for line in src:
            if not line.startswith(">"):
                out.write(line)
                continue

            header = line[1:].rstrip()
            parts = header.split(maxsplit=1)
            token = parts[0] if parts else ""
            suffix = f" {parts[1]}" if len(parts) > 1 else ""
            mapped = header_map.get(token)

            if mapped:
                out.write(f">{mapped}{suffix}\n")
                if mapped == token:
                    unchanged += 1
                else:
                    remapped += 1
            else:
                out.write(line)
                unmapped += 1

    logger.info(
        "  Wrote dna.fasta with remapped headers (%d remapped, %d unchanged, %d unmapped)",
        remapped,
        unchanged,
        unmapped,
    )


# ---------------------------------------------------------------------------
# Column listings for discovery
# ---------------------------------------------------------------------------

def list_columns(config: Dict[str, Any]) -> None:
    """Print column headers found in each configured source file."""
    source_files: Dict[str, str] = config.get("source_files", {})
    for table, path_str in source_files.items():
        path = Path(path_str)
        if not path.exists():
            print(f"  [{table}] NOT FOUND: {path}")
            continue
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            _ = next(iter(reader), None)  # advance to populate fieldnames
            cols = list(reader.fieldnames or [])
        print(f"\n  [{table}] {path} - {len(cols)} columns:")
        for c in cols:
            print(f"    {c}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

CASES_COLS = [
    "pseudonymised_case_id", "local_lab_sample_id", "specimen_date",
    "geographic_region", "case_status",
]
INTERP_COLS = [
    "sample_id", "species_confirmation", "lineage", "sublineage",
    "resistance_mutations", "predicted_drug_resistance",
    "confidence_score", "interpretation_summary",
]
QC_COLS = [
    "sample_id", "run_id", "mean_depth", "coverage_breadth",
    "ambiguous_base_percent", "contamination_flag", "qc_status",
    "qc_failure_reason", "reported_at",
]
RUNS_COLS = [
    "run_id", "platform", "instrument_name", "pipeline_version",
    "reference_genome", "started_at", "completed_at",
]
PROV_COLS = [
    "sample_id", "pipeline_name", "pipeline_version", "reference_genome",
    "software_versions", "parameters", "generated_at",
]


def run(args: argparse.Namespace) -> None:
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: config file not found: {config_path}")
        sys.exit(1)

    with config_path.open(encoding="utf-8") as fh:
        config: Dict[str, Any] = json.load(fh)

    if args.list_columns:
        print("\nColumn headers in configured source files:")
        list_columns(config)
        return

    out_dir = Path(args.out).resolve()
    source_files: Dict[str, str] = config.get("source_files", {})

    print(f"\nOutput directory: {out_dir}")
    print(f"Dry run         : {args.dry_run}")
    print()

    totals: Dict[str, int] = {}
    fasta_header_map: Dict[str, str] = {}

    # -- cases --------------------------------------------------------------
    cases_src = Path(source_files.get("cases", ""))
    if source_files.get("cases"):
        logger.info("Processing cases...")
        rows = _transform_csv(cases_src, config.get("cases", {}), CASES_COLS, args.dry_run)
        for row in rows:
            case_id = (row.get("pseudonymised_case_id") or "").strip()
            lab_id = (row.get("local_lab_sample_id") or "").strip()
            if case_id:
                fasta_header_map[case_id] = case_id
                if lab_id:
                    fasta_header_map[lab_id] = case_id
        if not args.dry_run and rows:
            n = _write_csv(rows, CASES_COLS, out_dir / "cases.csv")
            totals["cases.csv"] = n
            logger.info("  -> Wrote %d rows to cases.csv", n)

    # -- sequencing_runs ----------------------------------------------------
    runs_src = source_files.get("sequencing_runs", "")
    if runs_src:
        logger.info("Processing sequencing_runs...")
        rows = _transform_csv(Path(runs_src), config.get("sequencing_runs", {}), RUNS_COLS, args.dry_run)
        if not args.dry_run and rows:
            n = _write_csv(rows, RUNS_COLS, out_dir / "sequencing_runs.csv")
            totals["sequencing_runs.csv"] = n
            logger.info("  -> Wrote %d rows to sequencing_runs.csv", n)

    # -- tb_interpretation --------------------------------------------------
    interp_src = source_files.get("tb_interpretation", "")
    if interp_src:
        logger.info("Processing tb_interpretation...")
        rows = _transform_csv(Path(interp_src), config.get("tb_interpretation", {}), INTERP_COLS, args.dry_run)
        if not args.dry_run and rows:
            n = _write_csv(rows, INTERP_COLS, out_dir / "tb_interpretation.csv")
            totals["tb_interpretation.csv"] = n
            logger.info("  -> Wrote %d rows to tb_interpretation.csv", n)

    # -- sample_qc_metrics -------------------------------------------------
    qc_src = source_files.get("sample_qc_metrics", "")
    if qc_src:
        logger.info("Processing sample_qc_metrics...")
        rows = _transform_csv(Path(qc_src), config.get("sample_qc_metrics", {}), QC_COLS, args.dry_run)
        if not args.dry_run and rows:
            n = _write_csv(rows, QC_COLS, out_dir / "sample_qc_metrics.csv")
            totals["sample_qc_metrics.csv"] = n
            logger.info("  -> Wrote %d rows to sample_qc_metrics.csv", n)

    # -- analysis_provenance -----------------------------------------------
    prov_src = source_files.get("analysis_provenance", "")
    if prov_src:
        logger.info("Processing analysis_provenance...")
        rows = _transform_csv(Path(prov_src), config.get("analysis_provenance", {}), PROV_COLS, args.dry_run)
        if not args.dry_run and rows:
            n = _write_csv(rows, PROV_COLS, out_dir / "analysis_provenance.csv")
            totals["analysis_provenance.csv"] = n
            logger.info("  -> Wrote %d rows to analysis_provenance.csv", n)

    # -- FASTA -------------------------------------------------------------
    fasta_src = source_files.get("fasta", "")
    if fasta_src and not args.dry_run:
        logger.info("Preparing FASTA...")
        _rewrite_fasta_headers(Path(fasta_src), out_dir, fasta_header_map)

    if not args.dry_run:
        print("\n-- Output summary --------------------------------------")
        for fname, count in totals.items():
            print(f"  {fname}: {count} rows")
        print(f"\nBundle written to: {out_dir}")
        print("\nNext steps:")
        print(f"  1. python scripts/validate_ingest_files.py --dir {out_dir}")
        print(f"  2. python scripts/load_ingest_bundle.py --dir {out_dir}")
    else:
        print("\nDry run complete - no files written.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transform NI TB export files to the platform ingest bundle format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config",
        default="scripts/ni_column_map.json",
        help="Path to column mapping config JSON (default: scripts/ni_column_map.json)",
    )
    parser.add_argument(
        "--out",
        default="prepared_bundle",
        help="Output directory for the prepared bundle (default: prepared_bundle/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print a preview of the first 5 mapped rows per file without writing anything",
    )
    parser.add_argument(
        "--list-columns",
        action="store_true",
        help="Print column headers from each source file and exit (no transformation)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    run(args)


if __name__ == "__main__":
    main()

