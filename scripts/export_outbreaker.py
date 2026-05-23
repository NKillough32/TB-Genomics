import csv
import hashlib
import os
import sys
from datetime import date

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from backend.database import SessionLocal
from backend.data_safety import get_data_safety_status
try:
    from scripts.runtime_paths import EXPORTS
except ModuleNotFoundError:
    from runtime_paths import EXPORTS


def generate_consensus_sequence(case_id: str, length: int = 2000) -> str:
    # Deterministic pseudo-sequence to keep demo output stable across runs.
    bases = "ACGT"
    digest = hashlib.sha256(case_id.encode("utf-8")).digest()
    seed_values = list(digest)
    sequence_chars = []
    for i in range(length):
        value = seed_values[i % len(seed_values)]
        sequence_chars.append(bases[(value + i) % 4])
    return "".join(sequence_chars)


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "t", "yes", "y"}


def _num(value):
    try:
        return float(value)
    except Exception:
        return None


def _exclusion_reason(row, *, min_depth: float, min_coverage: float, max_ambiguous: float) -> tuple[str, str, str]:
    if not row.get("existing_sequence"):
        return "no_consensus_sequence", "sequence", "yes"
    qc_status = str(row.get("qc_status") or "").strip().lower()
    if qc_status in {"", "not_reported", "unknown", "na", "n/a"}:
        return "qc_not_reported", "qc_status", "yes"
    if qc_status not in {"pass", "passed"}:
        return "qc_status_fail", "qc_status", "yes"
    if _truthy(row.get("contamination_flag")):
        return "contamination_flag", "contamination_flag", "yes"
    mean_depth = _num(row.get("mean_depth"))
    if mean_depth is not None and mean_depth < min_depth:
        return "low_depth", "mean_depth", "yes"
    coverage = _num(row.get("coverage_breadth"))
    if coverage is not None and coverage < min_coverage:
        return "low_coverage_breadth", "coverage_breadth", "yes"
    ambiguous = _num(row.get("ambiguous_base_percent"))
    if ambiguous is not None and ambiguous > max_ambiguous:
        return "high_ambiguous_base_fraction", "ambiguous_base_percent", "yes"
    return "", "", "no"


def main() -> None:
    EXPORTS.mkdir(parents=True, exist_ok=True)

    db = SessionLocal()
    try:
        safety = get_data_safety_status(db)
        if (not safety["operational_safe"]) and os.getenv("TB_ALLOW_NON_OPERATIONAL_ACTIONS", "0") != "1":
            print("Blocked export_outbreaker: synthetic/demo dataset detected.")
            print(f"Data safety status: {safety}")
            print("Set TB_ALLOW_NON_OPERATIONAL_ACTIONS=1 to override for testing only.")
            sys.exit(2)

        min_depth = float(os.getenv("TB_QC_MIN_DEPTH", "20"))
        min_coverage = float(os.getenv("TB_QC_MIN_COVERAGE_BREADTH", "90"))
        max_ambiguous = float(os.getenv("TB_QC_MAX_AMBIGUOUS_BASE_PERCENT", "5"))

        rows = db.execute(
            text(
                """
                SELECT c.pseudonymised_case_id::text AS case_id,
                       c.specimen_date,
                       c.symptom_onset_date,
                       cs.sequence AS existing_sequence,
                       sqm.qc_status,
                       sqm.mean_depth,
                       sqm.coverage_breadth,
                       sqm.ambiguous_base_percent,
                       sqm.contamination_flag
                FROM cases c
                LEFT JOIN consensus_sequences cs
                    ON cs.sample_id = c.pseudonymised_case_id
                LEFT JOIN sample_qc_metrics sqm
                    ON sqm.sample_id = c.pseudonymised_case_id
                ORDER BY c.specimen_date ASC NULLS LAST
                """
            )
        ).mappings().all()

        cases_path = EXPORTS / "cases.csv"
        fasta_path = EXPORTS / "dna.fasta"
        qc_metrics_path = EXPORTS / "sample_qc_metrics.csv"
        excluded_path = EXPORTS / "outbreaker_excluded_samples.csv"

        eligible_rows = []
        excluded_rows = []
        for row in rows:
            reason, breached, repeat_required = _exclusion_reason(
                row,
                min_depth=min_depth,
                min_coverage=min_coverage,
                max_ambiguous=max_ambiguous,
            )
            if reason:
                excluded_rows.append(
                    {
                        "sample_id": row["case_id"],
                        "reason_excluded": reason,
                        "qc_parameter_breached": breached,
                        "qc_status": row.get("qc_status") or "not_reported",
                        "mean_depth": row.get("mean_depth"),
                        "coverage_breadth": row.get("coverage_breadth"),
                        "ambiguous_base_percent": row.get("ambiguous_base_percent"),
                        "contamination_flag": row.get("contamination_flag"),
                        "repeat_sequencing_required": repeat_required,
                    }
                )
            else:
                eligible_rows.append(row)

        with qc_metrics_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(
                csv_file,
                fieldnames=[
                    "sample_id",
                    "qc_status",
                    "mean_depth",
                    "coverage_breadth",
                    "ambiguous_base_percent",
                    "contamination_flag",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "sample_id": row["case_id"],
                        "qc_status": row.get("qc_status") or "not_reported",
                        "mean_depth": row.get("mean_depth"),
                        "coverage_breadth": row.get("coverage_breadth"),
                        "ambiguous_base_percent": row.get("ambiguous_base_percent"),
                        "contamination_flag": row.get("contamination_flag"),
                    }
                )

        with excluded_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(
                csv_file,
                fieldnames=[
                    "sample_id",
                    "reason_excluded",
                    "qc_parameter_breached",
                    "qc_status",
                    "mean_depth",
                    "coverage_breadth",
                    "ambiguous_base_percent",
                    "contamination_flag",
                    "repeat_sequencing_required",
                ],
            )
            writer.writeheader()
            writer.writerows(excluded_rows)

        with cases_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=["case_id", "sample_date", "symptom_onset_date"])
            writer.writeheader()

            for row in eligible_rows:
                sample_date = row["specimen_date"]
                if isinstance(sample_date, date):
                    sample_date_str = sample_date.isoformat()
                elif sample_date:
                    sample_date_str = str(sample_date)
                else:
                    sample_date_str = "2025-01-01"
                onset = row["symptom_onset_date"]
                onset_str = onset.isoformat() if isinstance(onset, date) else (str(onset) if onset else "")
                writer.writerow({"case_id": row["case_id"], "sample_date": sample_date_str, "symptom_onset_date": onset_str})

        raw_sequences = []
        for row in eligible_rows:
            sequence = (row["existing_sequence"] or generate_consensus_sequence(row["case_id"])).strip().upper()
            raw_sequences.append((row["case_id"], sequence))

        # outbreaker2 assumes all sequences are aligned to the same length.
        min_len = min((len(seq) for _, seq in raw_sequences), default=0)
        with fasta_path.open("w", encoding="utf-8") as fasta_file:
            for case_id, sequence in raw_sequences:
                normalized = sequence[:min_len] if min_len else sequence
                fasta_file.write(f">{case_id}\n")
                fasta_file.write(f"{normalized}\n")

        print(
            f"Exported {len(eligible_rows)} QC-pass records to {cases_path} and {fasta_path}; "
            f"excluded {len(excluded_rows)} sample(s) in {excluded_path}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()

