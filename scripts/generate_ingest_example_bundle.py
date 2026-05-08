#!/usr/bin/env python3
"""Generate an ingest-ready example data bundle for user onboarding.

The bundle is intentionally non-identifiable and uses public TB incidence
data from the World Bank API to make region distributions realistic.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import urlopen

WORLD_BANK_SOURCE_URL = (
    "https://api.worldbank.org/v2/country/"
    "GBR,IRL,IND,ZAF,NGA,PAK,BGD,PHL,IDN,CHN,PER,UKR/"
    "indicator/SH.TBS.INCD?format=json&per_page=20000"
)

FALLBACK_INCIDENCE = {
    "GBR": {"country": "United Kingdom", "incidence": 7.0},
    "IRL": {"country": "Ireland", "incidence": 8.0},
    "IND": {"country": "India", "incidence": 199.0},
    "ZAF": {"country": "South Africa", "incidence": 468.0},
    "NGA": {"country": "Nigeria", "incidence": 219.0},
    "PAK": {"country": "Pakistan", "incidence": 263.0},
    "BGD": {"country": "Bangladesh", "incidence": 221.0},
    "PHL": {"country": "Philippines", "incidence": 554.0},
    "IDN": {"country": "Indonesia", "incidence": 354.0},
    "CHN": {"country": "China", "incidence": 55.0},
    "PER": {"country": "Peru", "incidence": 116.0},
    "UKR": {"country": "Ukraine", "incidence": 71.0},
}

CASE_STATUSES = ["confirmed", "probable", "under_review"]
LINEAGES = ["L1", "L2", "L3", "L4"]


def fetch_latest_incidence() -> tuple[dict[str, dict[str, float]], bool]:
    try:
        with urlopen(WORLD_BANK_SOURCE_URL, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        entries = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
        latest: dict[str, dict[str, float]] = {}
        for row in entries:
            iso3 = row.get("countryiso3code")
            value = row.get("value")
            year_raw = row.get("date")
            country_info = row.get("country") or {}
            country_name = country_info.get("value", iso3)
            if not iso3 or value is None or not year_raw:
                continue
            year = int(year_raw)
            prev = latest.get(iso3)
            if prev is None or year > prev["year"]:
                latest[iso3] = {
                    "country": country_name,
                    "incidence": float(value),
                    "year": year,
                }
        if not latest:
            return FALLBACK_INCIDENCE, True
        clean = {
            iso3: {"country": d["country"], "incidence": d["incidence"]}
            for iso3, d in latest.items()
        }
        return clean, False
    except Exception:
        return FALLBACK_INCIDENCE, True


def build_sampler(incidence_data: dict[str, dict[str, float]]) -> list[tuple[str, float]]:
    total = sum(max(0.1, d["incidence"]) for d in incidence_data.values())
    cumulative = 0.0
    out: list[tuple[str, float]] = []
    for iso3, d in incidence_data.items():
        cumulative += max(0.1, d["incidence"]) / total
        out.append((iso3, cumulative))
    if out:
        out[-1] = (out[-1][0], 1.0)
    return out


def pick_country(sampler: list[tuple[str, float]]) -> str:
    r = random.random()
    for iso3, cutoff in sampler:
        if r <= cutoff:
            return iso3
    return sampler[-1][0]


def generate_consensus_sequence(case_id: str, length: int = 1200) -> str:
    bases = "ACGT"
    numbers = list(uuid.UUID(case_id).bytes)
    chars = []
    for i in range(length):
        chars.append(bases[(numbers[i % len(numbers)] + i) % 4])
    return "".join(chars)


def lineage_for_incidence(incidence: float) -> str:
    if incidence >= 300:
        return random.choices(LINEAGES, weights=[0.3, 0.35, 0.2, 0.15], k=1)[0]
    if incidence >= 100:
        return random.choices(LINEAGES, weights=[0.2, 0.3, 0.2, 0.3], k=1)[0]
    return random.choices(LINEAGES, weights=[0.1, 0.15, 0.15, 0.6], k=1)[0]


def make_resistance_profile(incidence: float) -> tuple[dict[str, str], list[dict[str, str]]]:
    drugs = ["isoniazid", "rifampicin", "ethambutol", "pyrazinamide"]
    resistant_chance = min(0.45, 0.08 + (incidence / 1200.0))
    predicted: dict[str, str] = {}
    mutations: list[dict[str, str]] = []
    for drug in drugs:
        resistant = random.random() < resistant_chance
        predicted[drug] = "resistant" if resistant else "susceptible"
        if resistant:
            mutations.append(
                {
                    "gene": random.choice(["rpoB", "katG", "inhA", "embB", "pncA"]),
                    "variant": random.choice(["S315T", "D516V", "H526Y", "S531L", "C15T"]),
                    "drug": drug,
                }
            )
    return predicted, mutations


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


AVAILABLE_COUNTRIES = {d["country"] for d in FALLBACK_INCIDENCE.values()}
AVAILABLE_ISO3 = set(FALLBACK_INCIDENCE.keys())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate ingest-ready example files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Available countries (use exact name or ISO3 code):\n  "
            + "\n  ".join(
                sorted(f"{iso3}: {d['country']}" for iso3, d in FALLBACK_INCIDENCE.items())
            )
        ),
    )
    parser.add_argument("--cases", type=int, default=30, help="Number of example cases")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument(
        "--output-dir",
        default="examples/ingest_bundle",
        help="Directory where bundle files are written",
    )
    parser.add_argument(
        "--countries",
        nargs="+",
        metavar="COUNTRY_OR_ISO3",
        help=(
            "Filter to specific countries. Accepts ISO3 codes (e.g. GBR IRL) or full country "
            "names (e.g. \"United Kingdom\"). Case-insensitive. Multiple values allowed."
        ),
    )
    args = parser.parse_args()

    random.seed(args.seed)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    incidence_data, used_fallback = fetch_latest_incidence()

    if args.countries:
        # Build a lookup of both ISO3 codes and country names (lower-cased) for flexible matching.
        requested = [c.strip().lower() for c in args.countries]
        filtered = {
            iso3: d
            for iso3, d in incidence_data.items()
            if iso3.lower() in requested or d["country"].lower() in requested
        }
        if not filtered:
            available = ", ".join(
                f"{iso3} ({d['country']})" for iso3, d in sorted(incidence_data.items())
            )
            print(
                f"ERROR: None of the specified countries matched available data.\n"
                f"Available: {available}",
                file=sys.stderr,
            )
            sys.exit(2)
        unmatched = [
            c for c in requested
            if c not in {k.lower() for k in filtered}
            and c not in {d["country"].lower() for d in filtered.values()}
        ]
        if unmatched:
            print(
                f"WARNING: The following countries were not found and will be skipped: {unmatched}",
                file=sys.stderr,
            )
        incidence_data = filtered

    sampler = build_sampler(incidence_data)

    run_id = f"RUN-{datetime.utcnow():%Y%m%d}-EXAMPLE"
    now = datetime.utcnow()
    started_at = now - timedelta(hours=5)

    cases_rows: list[dict[str, object]] = []
    interpretation_rows: list[dict[str, object]] = []
    qc_rows: list[dict[str, object]] = []
    provenance_rows: list[dict[str, object]] = []
    fasta_records: list[tuple[str, str]] = []

    for i in range(1, args.cases + 1):
        iso3 = pick_country(sampler)
        country = incidence_data[iso3]["country"]
        incidence = incidence_data[iso3]["incidence"]
        specimen_date = date.today() - timedelta(days=random.randint(0, 540))
        case_id = str(uuid.uuid4())
        local_sample_id = f"LAB-{date.today().year}-{i:05d}"
        case_status = random.choices(CASE_STATUSES, weights=[0.78, 0.17, 0.05], k=1)[0]

        lineage = lineage_for_incidence(incidence)
        sublineage = f"{lineage}.{random.randint(1, 9)}"
        predicted, mutations = make_resistance_profile(incidence)
        conf = round(random.uniform(0.76, 0.99), 3)
        sequence = generate_consensus_sequence(case_id)
        qc_pass = random.random() > 0.15

        cases_rows.append(
            {
                "pseudonymised_case_id": case_id,
                "local_lab_sample_id": local_sample_id,
                "specimen_date": specimen_date.isoformat(),
                "geographic_region": country,
                "case_status": case_status,
            }
        )

        interpretation_rows.append(
            {
                "sample_id": case_id,
                "species_confirmation": "M. tuberculosis complex",
                "lineage": lineage,
                "sublineage": sublineage,
                "resistance_mutations": json.dumps(mutations),
                "predicted_drug_resistance": json.dumps(predicted),
                "confidence_score": conf,
                "interpretation_summary": f"Example interpretation based on public incidence trend for {country}.",
            }
        )

        qc_rows.append(
            {
                "sample_id": case_id,
                "run_id": run_id,
                "mean_depth": round(random.uniform(38, 120), 2),
                "coverage_breadth": round(random.uniform(93, 99.9), 2),
                "ambiguous_base_percent": round(random.uniform(0.1, 2.8), 2),
                "contamination_flag": "true" if random.random() < 0.06 else "false",
                "qc_status": "pass" if qc_pass else "fail",
                "qc_failure_reason": "" if qc_pass else random.choice(["low_depth", "high_ambiguity"]),
                "reported_at": (now - timedelta(days=random.randint(0, 30))).isoformat(),
            }
        )

        provenance_rows.append(
            {
                "sample_id": case_id,
                "pipeline_name": "tb-wgs-pipeline",
                "pipeline_version": "1.4.0",
                "reference_genome": "H37Rv",
                "software_versions": json.dumps(
                    {"bwa": "0.7.17", "samtools": "1.19", "freebayes": "1.3.7"}
                ),
                "parameters": json.dumps({"min_depth": 10, "min_qual": 30, "mask_pe_ppe": True}),
                "generated_at": now.isoformat(),
            }
        )

        fasta_records.append((case_id, sequence))

    sequencing_runs_rows = [
        {
            "run_id": run_id,
            "platform": "Illumina",
            "instrument_name": "MiSeq",
            "pipeline_version": "tb-wgs-pipeline-1.4.0",
            "reference_genome": "H37Rv",
            "started_at": started_at.isoformat(),
            "completed_at": now.isoformat(),
        }
    ]

    write_csv(
        output_dir / "cases.csv",
        [
            "pseudonymised_case_id",
            "local_lab_sample_id",
            "specimen_date",
            "geographic_region",
            "case_status",
        ],
        cases_rows,
    )
    write_csv(
        output_dir / "tb_interpretation.csv",
        [
            "sample_id",
            "species_confirmation",
            "lineage",
            "sublineage",
            "resistance_mutations",
            "predicted_drug_resistance",
            "confidence_score",
            "interpretation_summary",
        ],
        interpretation_rows,
    )
    write_csv(
        output_dir / "sequencing_runs.csv",
        [
            "run_id",
            "platform",
            "instrument_name",
            "pipeline_version",
            "reference_genome",
            "started_at",
            "completed_at",
        ],
        sequencing_runs_rows,
    )
    write_csv(
        output_dir / "sample_qc_metrics.csv",
        [
            "sample_id",
            "run_id",
            "mean_depth",
            "coverage_breadth",
            "ambiguous_base_percent",
            "contamination_flag",
            "qc_status",
            "qc_failure_reason",
            "reported_at",
        ],
        qc_rows,
    )
    write_csv(
        output_dir / "analysis_provenance.csv",
        [
            "sample_id",
            "pipeline_name",
            "pipeline_version",
            "reference_genome",
            "software_versions",
            "parameters",
            "generated_at",
        ],
        provenance_rows,
    )

    with (output_dir / "dna.fasta").open("w", encoding="utf-8") as f:
        for sample_id, seq in fasta_records:
            f.write(f">{sample_id}\n")
            f.write(f"{seq}\n")

    manifest = {
        "generated_at": datetime.utcnow().isoformat(),
        "source": WORLD_BANK_SOURCE_URL,
        "used_fallback_incidence_values": used_fallback,
        "countries_included": sorted(d["country"] for d in incidence_data.values()),
        "notes": [
            "All records are synthetic and non-identifiable.",
            "Use these files as shape templates for your own data mapping.",
            "Join key across files is pseudonymised_case_id/sample_id.",
        ],
        "files": {
            "cases.csv": "Core case records for cases table",
            "tb_interpretation.csv": "Lineage and resistance interpretation",
            "sequencing_runs.csv": "Run metadata",
            "sample_qc_metrics.csv": "QC metrics linked to sample and run",
            "analysis_provenance.csv": "Pipeline/provenance metadata",
            "dna.fasta": "Consensus sequences named by sample_id",
        },
    }
    with (output_dir / "ingest_manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(
        json.dumps(
            {
                "status": "ok",
                "output_dir": str(output_dir),
                "cases": args.cases,
                "countries": sorted(d["country"] for d in incidence_data.values()),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
