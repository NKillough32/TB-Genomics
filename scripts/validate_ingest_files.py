#!/usr/bin/env python3
"""Validate user-supplied ingest files before loading into the TB platform.

Checks column presence, data types, join integrity, FASTA header alignment,
and common formatting issues. Prints a clear report with PASS / WARN / FAIL
per check and exits non-zero if any FAIL is detected.

Usage (from project root):
    python scripts/validate_ingest_files.py --dir path/to/your/data

Example against the bundled example files:
    python scripts/validate_ingest_files.py --dir examples/ingest_bundle
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Expected schema definitions
# ---------------------------------------------------------------------------

CASES_REQUIRED = {
    "pseudonymised_case_id": "uuid",
    "local_lab_sample_id": "str",
    "specimen_date": "date",
    "geographic_region": "str",
    "case_status": "str",
}

CASES_VALID_STATUSES = {"confirmed", "probable", "under_review"}

INTERPRETATION_REQUIRED = {
    "sample_id": "uuid",
    "species_confirmation": "str",
    "lineage": "str",
    "confidence_score": "float",
}

INTERPRETATION_VALID_LINEAGES = {"L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9", "Bovis", "Caprae"}

QC_REQUIRED = {
    "sample_id": "uuid",
    "run_id": "str",
    "mean_depth": "float",
    "coverage_breadth": "float",
    "qc_status": "str",
}

QC_VALID_STATUSES = {"pass", "fail", "passed", "failed"}

RUNS_REQUIRED = {
    "run_id": "str",
    "platform": "str",
}

PROVENANCE_REQUIRED = {
    "sample_id": "uuid",
    "pipeline_name": "str",
    "pipeline_version": "str",
    "reference_genome": "str",
}

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
FASTA_HEADER_RE = re.compile(r"^>(.+)$")
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d")


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

_findings: list[tuple[str, str, str]] = []
_fail_count = 0
_warn_count = 0


def reset_findings() -> None:
    global _fail_count, _warn_count
    _findings.clear()
    _fail_count = 0
    _warn_count = 0


def report(level: str, check: str, message: str) -> None:
    global _fail_count, _warn_count
    _findings.append((level, check, message))
    if level == FAIL:
        _fail_count += 1
    elif level == WARN:
        _warn_count += 1


def ok(check: str, message: str) -> None:
    report(PASS, check, message)


def warn(check: str, message: str) -> None:
    report(WARN, check, message)


def fail(check: str, message: str) -> None:
    report(FAIL, check, message)


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_csv(path: Path) -> tuple[list[str], list[dict[str, str]]] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return fieldnames, rows


def check_columns(
    name: str,
    fieldnames: list[str],
    required: dict[str, str],
) -> None:
    missing = [col for col in required if col not in fieldnames]
    if missing:
        fail(f"{name}:columns", f"Missing required columns: {missing}")
    else:
        ok(f"{name}:columns", f"All {len(required)} required columns present")


def is_uuid(value: str) -> bool:
    return bool(UUID_RE.match(value.strip())) if value else False


def is_date(value: str) -> bool:
    v = value.strip()
    if not v:
        return False
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(v, fmt)
            return True
        except ValueError:
            continue
    return False


def is_float(value: str) -> bool:
    try:
        float(value.strip())
        return True
    except (ValueError, AttributeError):
        return False


def is_json(value: str) -> bool:
    try:
        json.loads(value.strip())
        return True
    except (ValueError, AttributeError):
        return False


def type_check(
    name: str,
    rows: list[dict[str, str]],
    schema: dict[str, str],
) -> None:
    type_errors: list[str] = []
    for i, row in enumerate(rows, start=2):  # row 1 is header
        for col, dtype in schema.items():
            val = row.get(col, "")
            if not val:
                continue
            if dtype == "uuid" and not is_uuid(val):
                type_errors.append(f"row {i} {col}: not a valid UUID ({val[:40]})")
            elif dtype == "date" and not is_date(val):
                type_errors.append(f"row {i} {col}: not a supported date ({val[:20]})")
            elif dtype == "float" and not is_float(val):
                type_errors.append(f"row {i} {col}: not numeric ({val[:20]})")

    if type_errors:
        sample = type_errors[:5]
        extras = len(type_errors) - len(sample)
        msg = "; ".join(sample)
        if extras > 0:
            msg += f" ... and {extras} more"
        fail(f"{name}:types", msg)
    else:
        ok(f"{name}:types", "All sampled type checks passed")


def check_blanks(
    name: str,
    rows: list[dict[str, str]],
    required_cols: list[str],
) -> None:
    blanks: list[str] = []
    for i, row in enumerate(rows, start=2):
        for col in required_cols:
            if not row.get(col, "").strip():
                blanks.append(f"row {i} {col}")
    if blanks:
        sample = blanks[:6]
        extras = len(blanks) - len(sample)
        msg = "; ".join(sample)
        if extras > 0:
            msg += f" ... and {extras} more"
        fail(f"{name}:blanks", f"Blank values in required columns: {msg}")
    else:
        ok(f"{name}:blanks", "No blank values in required columns")


# ---------------------------------------------------------------------------
# Individual file checks
# ---------------------------------------------------------------------------

def validate_cases(path: Path) -> set[str]:
    result = load_csv(path)
    if result is None:
        fail("cases.csv:exists", "File not found")
        return set()
    fieldnames, rows = result
    ok("cases.csv:exists", f"Found with {len(rows)} rows")

    check_columns("cases.csv", fieldnames, CASES_REQUIRED)
    type_check("cases.csv", rows, CASES_REQUIRED)
    check_blanks("cases.csv", rows, list(CASES_REQUIRED.keys()))

    bad_status = [
        r.get("case_status", "") for r in rows
        if r.get("case_status", "").strip().lower() not in CASES_VALID_STATUSES
    ]
    if bad_status:
        warn(
            "cases.csv:status_values",
            f"{len(bad_status)} unexpected case_status values (e.g. '{bad_status[0]}'). "
            f"Expected: {CASES_VALID_STATUSES}",
        )
    else:
        ok("cases.csv:status_values", "All case_status values valid")

    ids = {r.get("pseudonymised_case_id", "").strip() for r in rows}
    duplicates = len(rows) - len(ids)
    if duplicates:
        fail("cases.csv:unique_ids", f"{duplicates} duplicate pseudonymised_case_id values")
    else:
        ok("cases.csv:unique_ids", "All pseudonymised_case_id values unique")

    return ids


def missing_file(check: str, message: str, strict: bool) -> None:
    if strict:
        fail(check, f"{message} (required in --strict-analysis mode)")
    else:
        warn(check, message)


def validate_interpretation(path: Path, case_ids: set[str], strict: bool = False) -> None:
    result = load_csv(path)
    if result is None:
        missing_file(
            "tb_interpretation.csv:exists",
            "File not found (optional but recommended)",
            strict,
        )
        return
    fieldnames, rows = result
    ok("tb_interpretation.csv:exists", f"Found with {len(rows)} rows")

    check_columns("tb_interpretation.csv", fieldnames, INTERPRETATION_REQUIRED)
    type_check("tb_interpretation.csv", rows, INTERPRETATION_REQUIRED)
    check_blanks("tb_interpretation.csv", rows, list(INTERPRETATION_REQUIRED.keys()))

    orphan_ids = [
        r.get("sample_id", "").strip() for r in rows
        if r.get("sample_id", "").strip() not in case_ids
    ]
    if orphan_ids:
        sample = orphan_ids[:3]
        fail(
            "tb_interpretation.csv:fk",
            f"{len(orphan_ids)} sample_id values not in cases.csv (e.g. {sample})",
        )
    else:
        ok("tb_interpretation.csv:fk", "All sample_id values reference known cases")

    bad_lineage = [
        r.get("lineage", "") for r in rows
        if r.get("lineage", "").strip() not in INTERPRETATION_VALID_LINEAGES
        and r.get("lineage", "").strip()
    ]
    if bad_lineage:
        warn(
            "tb_interpretation.csv:lineage",
            f"{len(bad_lineage)} unrecognised lineage values (e.g. '{bad_lineage[0]}'). "
            f"Standard values: {INTERPRETATION_VALID_LINEAGES}",
        )
    else:
        ok("tb_interpretation.csv:lineage", "Lineage values match expected set")

    for col in ("resistance_mutations", "predicted_drug_resistance"):
        if col not in fieldnames:
            continue
        bad_json = [i + 2 for i, r in enumerate(rows) if r.get(col) and not is_json(r[col])]
        if bad_json:
            fail(
                f"tb_interpretation.csv:{col}",
                f"Invalid JSON in column '{col}' at rows: {bad_json[:5]}",
            )
        else:
            ok(f"tb_interpretation.csv:{col}", f"Column '{col}' contains valid JSON")


def validate_sequencing_runs(path: Path, strict: bool = False) -> set[str]:
    result = load_csv(path)
    if result is None:
        missing_file(
            "sequencing_runs.csv:exists",
            "File not found (required if using QC metrics)",
            strict,
        )
        return set()
    fieldnames, rows = result
    ok("sequencing_runs.csv:exists", f"Found with {len(rows)} rows")

    check_columns("sequencing_runs.csv", fieldnames, RUNS_REQUIRED)
    check_blanks("sequencing_runs.csv", rows, list(RUNS_REQUIRED.keys()))

    run_ids = {r.get("run_id", "").strip() for r in rows}
    return run_ids


def validate_qc(path: Path, case_ids: set[str], run_ids: set[str], strict: bool = False) -> None:
    result = load_csv(path)
    if result is None:
        missing_file(
            "sample_qc_metrics.csv:exists",
            "File not found (required for KPI reporting)",
            strict,
        )
        return
    fieldnames, rows = result
    ok("sample_qc_metrics.csv:exists", f"Found with {len(rows)} rows")

    check_columns("sample_qc_metrics.csv", fieldnames, QC_REQUIRED)
    type_check("sample_qc_metrics.csv", rows, QC_REQUIRED)
    check_blanks("sample_qc_metrics.csv", rows, list(QC_REQUIRED.keys()))

    orphan_ids = [
        r.get("sample_id", "").strip() for r in rows
        if r.get("sample_id", "").strip() not in case_ids
    ]
    if orphan_ids:
        fail(
            "sample_qc_metrics.csv:fk_case",
            f"{len(orphan_ids)} sample_id values not in cases.csv",
        )
    else:
        ok("sample_qc_metrics.csv:fk_case", "All sample_id values reference known cases")

    if run_ids:
        orphan_runs = [
            r.get("run_id", "").strip() for r in rows
            if r.get("run_id", "").strip() and r.get("run_id", "").strip() not in run_ids
        ]
        if orphan_runs:
            fail(
                "sample_qc_metrics.csv:fk_run",
                f"{len(orphan_runs)} run_id values not in sequencing_runs.csv",
            )
        else:
            ok("sample_qc_metrics.csv:fk_run", "All run_id values reference known runs")

    bad_status = [
        r.get("qc_status", "") for r in rows
        if r.get("qc_status", "").strip().lower() not in QC_VALID_STATUSES
        and r.get("qc_status", "").strip()
    ]
    if bad_status:
        warn(
            "sample_qc_metrics.csv:qc_status",
            f"{len(bad_status)} unexpected qc_status values. "
            f"Expected one of: {QC_VALID_STATUSES}",
        )
    else:
        ok("sample_qc_metrics.csv:qc_status", "All qc_status values valid")

    low_depth = [r for r in rows if is_float(r.get("mean_depth", "")) and float(r["mean_depth"]) < 20]
    if low_depth:
        warn(
            "sample_qc_metrics.csv:low_depth",
            f"{len(low_depth)} sample(s) have mean_depth < 20x (may cause QC failures)",
        )
    else:
        ok("sample_qc_metrics.csv:low_depth", "All mean_depth values >= 20x")


def validate_provenance(path: Path, case_ids: set[str], strict: bool = False) -> None:
    result = load_csv(path)
    if result is None:
        missing_file(
            "analysis_provenance.csv:exists",
            "File not found (recommended for audit trail)",
            strict,
        )
        return
    fieldnames, rows = result
    ok("analysis_provenance.csv:exists", f"Found with {len(rows)} rows")

    check_columns("analysis_provenance.csv", fieldnames, PROVENANCE_REQUIRED)
    type_check("analysis_provenance.csv", rows, {"sample_id": "uuid"})

    orphan_ids = [
        r.get("sample_id", "").strip() for r in rows
        if r.get("sample_id", "").strip() not in case_ids
    ]
    if orphan_ids:
        fail(
            "analysis_provenance.csv:fk",
            f"{len(orphan_ids)} sample_id values not in cases.csv",
        )
    else:
        ok("analysis_provenance.csv:fk", "All sample_id values reference known cases")


def validate_fasta(path: Path, case_ids: set[str], strict: bool = False) -> None:
    if not path.exists():
        missing_file(
            "dna.fasta:exists",
            "File not found (required for outbreaker2 analysis)",
            strict,
        )
        return

    headers: list[str] = []
    seq_lengths: list[int] = []
    current_len = 0
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.rstrip()
            if line.startswith(">"):
                if current_len > 0:
                    seq_lengths.append(current_len)
                    current_len = 0
                m = FASTA_HEADER_RE.match(line)
                if m:
                    headers.append(m.group(1).strip().split()[0])
            elif line:
                current_len += len(line)
        if current_len > 0:
            seq_lengths.append(current_len)

    ok("dna.fasta:exists", f"Found with {len(headers)} sequences")

    if not headers:
        fail("dna.fasta:sequences", "No sequences found in FASTA file")
        return

    if case_ids:
        missing_seqs = case_ids - set(headers)
        extra_seqs = set(headers) - case_ids
        if missing_seqs:
            sample = list(missing_seqs)[:3]
            reporter = fail if strict else warn
            reporter(
                "dna.fasta:coverage",
                f"{len(missing_seqs)} cases in cases.csv have no sequence in FASTA "
                f"(e.g. {sample})",
            )
        else:
            ok("dna.fasta:coverage", "All cases in cases.csv have a FASTA sequence")

        if extra_seqs:
            reporter = fail if strict else warn
            reporter(
                "dna.fasta:orphan_seqs",
                f"{len(extra_seqs)} FASTA headers not found in cases.csv "
                f"(will be ignored at ingest)",
            )

    if seq_lengths:
        min_len = min(seq_lengths)
        max_len = max(seq_lengths)
        if min_len < 500:
            warn("dna.fasta:seq_length", f"Shortest sequence is {min_len} bp (unusually short)")
        else:
            ok("dna.fasta:seq_length", f"Sequence lengths: min={min_len} bp, max={max_len} bp")

        if max_len - min_len > 500:
            warn(
                "dna.fasta:length_variance",
                f"Large length variance: min={min_len} bp, max={max_len} bp. "
                "Check alignment/trimming.",
            )


def validate_bundle(input_dir: Path, strict_analysis: bool = False) -> dict:
    """Validate a prepared ingest bundle and return structured findings."""
    reset_findings()
    case_ids = validate_cases(input_dir / "cases.csv")
    run_ids = validate_sequencing_runs(input_dir / "sequencing_runs.csv", strict_analysis)
    validate_interpretation(input_dir / "tb_interpretation.csv", case_ids, strict_analysis)
    validate_qc(input_dir / "sample_qc_metrics.csv", case_ids, run_ids, strict_analysis)
    validate_provenance(input_dir / "analysis_provenance.csv", case_ids, strict_analysis)
    validate_fasta(input_dir / "dna.fasta", case_ids, strict_analysis)
    return {
        "summary": {
            "pass": sum(1 for f in _findings if f[0] == PASS),
            "warn": _warn_count,
            "fail": _fail_count,
        },
        "findings": [{"level": l, "check": c, "message": m} for l, c, m in _findings],
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Validate TB ingest files")
    parser.add_argument(
        "--dir",
        default="examples/ingest_bundle",
        help="Directory containing files to validate",
    )
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument(
        "--strict-analysis",
        action="store_true",
        help=(
            "Fail when analysis-critical optional files are missing or FASTA coverage "
            "does not match cases.csv"
        ),
    )
    args = parser.parse_args()

    input_dir = Path(args.dir)
    if not input_dir.is_dir():
        print(f"ERROR: directory not found: {input_dir}", file=sys.stderr)
        sys.exit(2)

    output = validate_bundle(input_dir, strict_analysis=args.strict_analysis)

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        col_widths = (6, 45, 0)
        print(f"\n{'Level':<6}  {'Check':<45}  Message")
        print("-" * 100)
        for level, check, message in _findings:
            tag = {"PASS": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[level]
            print(f"{tag:<6}  {check:<45}  {message}")
        print("-" * 100)
        print(
            f"\nResult: {sum(1 for f in _findings if f[0] == PASS)} passed  "
            f"/ {_warn_count} warnings  / {_fail_count} failures"
        )
        if _fail_count > 0:
            print("\nOne or more checks FAILED. Fix issues above before loading data.")
            sys.exit(1)
        elif _warn_count > 0:
            print("\nAll checks passed with warnings. Review warnings before loading data.")
        else:
            print("\nAll checks passed. Files are ready to load.")


if __name__ == "__main__":
    main()

