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


def main() -> None:
    os.makedirs("exports", exist_ok=True)

    db = SessionLocal()
    try:
        safety = get_data_safety_status(db)
        if (not safety["operational_safe"]) and os.getenv("TB_ALLOW_NON_OPERATIONAL_ACTIONS", "0") != "1":
            print("Blocked export_outbreaker: synthetic/demo dataset detected.")
            print(f"Data safety status: {safety}")
            print("Set TB_ALLOW_NON_OPERATIONAL_ACTIONS=1 to override for testing only.")
            sys.exit(2)

        rows = db.execute(
            text(
                """
                SELECT c.pseudonymised_case_id::text AS case_id,
                       c.specimen_date,
                       cs.sequence AS existing_sequence
                FROM cases c
                LEFT JOIN consensus_sequences cs
                    ON cs.sample_id = c.pseudonymised_case_id
                ORDER BY c.specimen_date ASC NULLS LAST
                """
            )
        ).mappings().all()

        with open("exports/cases.csv", "w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=["case_id", "sample_date"])
            writer.writeheader()

            for row in rows:
                sample_date = row["specimen_date"]
                if isinstance(sample_date, date):
                    sample_date_str = sample_date.isoformat()
                elif sample_date:
                    sample_date_str = str(sample_date)
                else:
                    sample_date_str = "2025-01-01"
                writer.writerow({"case_id": row["case_id"], "sample_date": sample_date_str})

        raw_sequences = []
        for row in rows:
            sequence = (row["existing_sequence"] or generate_consensus_sequence(row["case_id"])).strip().upper()
            raw_sequences.append((row["case_id"], sequence))

        # outbreaker2 assumes all sequences are aligned to the same length.
        min_len = min((len(seq) for _, seq in raw_sequences), default=0)
        with open("exports/dna.fasta", "w", encoding="utf-8") as fasta_file:
            for case_id, sequence in raw_sequences:
                normalized = sequence[:min_len] if min_len else sequence
                fasta_file.write(f">{case_id}\n")
                fasta_file.write(f"{normalized}\n")

        print(
            f"Exported {len(rows)} records to exports/cases.csv and exports/dna.fasta"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
