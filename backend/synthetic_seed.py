import argparse
import json
import random
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple
from urllib.request import urlopen

from sqlalchemy import text

from backend.database import SessionLocal
from backend.models import Case

WORLD_BANK_SOURCE_URL = (
    "https://api.worldbank.org/v2/country/"
    "GBR,IRL,IND,ZAF,NGA,PAK,BGD,PHL,IDN,CHN,PER,UKR/"
    "indicator/SH.TBS.INCD?format=json&per_page=20000"
)

# Fallback values (incidence per 100k) used only if online fetch fails.
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

DRUGS = ["isoniazid", "rifampicin", "ethambutol", "pyrazinamide", "fluoroquinolones"]
LINEAGES = ["L1", "L2", "L3", "L4"]
NUCLEOTIDES = ["A", "C", "G", "T"]


def _fetch_latest_incidence() -> Tuple[Dict[str, Dict[str, float]], bool]:
    try:
        with urlopen(WORLD_BANK_SOURCE_URL, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))

        by_country: Dict[str, Dict[str, float]] = {}
        for row in payload[1]:
            iso3 = row.get("countryiso3code")
            value = row.get("value")
            country_info = row.get("country") or {}
            country_name = country_info.get("value", iso3)
            year_raw = row.get("date")
            if not iso3 or value is None or not year_raw:
                continue

            year = int(year_raw)
            previous = by_country.get(iso3)
            if previous is None or year > previous["year"]:
                by_country[iso3] = {
                    "country": country_name,
                    "incidence": float(value),
                    "year": year,
                }

        cleaned = {
            iso3: {"country": item["country"], "incidence": item["incidence"]}
            for iso3, item in by_country.items()
        }
        if not cleaned:
            return FALLBACK_INCIDENCE, True
        return cleaned, False
    except Exception:
        return FALLBACK_INCIDENCE, True


def _weighted_country_sampler(incidence_data: Dict[str, Dict[str, float]]) -> List[Tuple[str, float]]:
    weighted = []
    total = sum(max(0.1, row["incidence"]) for row in incidence_data.values())
    running = 0.0
    for iso3, row in incidence_data.items():
        running += max(0.1, row["incidence"]) / total
        weighted.append((iso3, running))
    weighted[-1] = (weighted[-1][0], 1.0)
    return weighted


def _pick_country(weighted_sampler: List[Tuple[str, float]]) -> str:
    roll = random.random()
    for iso3, cumulative in weighted_sampler:
        if roll <= cumulative:
            return iso3
    return weighted_sampler[-1][0]


def _lineage_for_incidence(incidence: float) -> str:
    if incidence >= 300:
        return random.choices(LINEAGES, weights=[0.3, 0.35, 0.2, 0.15], k=1)[0]
    if incidence >= 100:
        return random.choices(LINEAGES, weights=[0.2, 0.3, 0.2, 0.3], k=1)[0]
    return random.choices(LINEAGES, weights=[0.1, 0.15, 0.15, 0.6], k=1)[0]


def _build_resistance_profile(incidence: float) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    # Higher-incidence settings get a slightly elevated chance of resistance signals.
    resistant_chance = min(0.45, 0.08 + (incidence / 1200.0))
    predicted = {}
    mutations = []
    for drug in DRUGS:
        is_resistant = random.random() < resistant_chance
        predicted[drug] = "resistant" if is_resistant else "susceptible"
        if is_resistant:
            mutation = {
                "gene": random.choice(["rpoB", "katG", "inhA", "embB", "pncA", "gyrA"]),
                "variant": random.choice(["S315T", "D516V", "H526Y", "S531L", "C15T", "Q431K"]),
                "drug": drug,
            }
            mutations.append(mutation)
    return predicted, mutations


def _build_consensus_sequence(incidence: float) -> str:
    # Keep synthetic consensus compact for dev performance while preserving variability.
    seq_len = 1200
    base_weights = [0.25, 0.25, 0.25, 0.25]
    # Add a slight GC tilt for higher-incidence settings to diversify synthetic patterns.
    if incidence >= 250:
        base_weights = [0.22, 0.28, 0.28, 0.22]
    return "".join(random.choices(NUCLEOTIDES, weights=base_weights, k=seq_len))


def _mutate_sequence(base_sequence: str, mutation_rate: float) -> str:
    chars = list(base_sequence)
    for i, current in enumerate(chars):
        if random.random() < mutation_rate:
            choices = [b for b in NUCLEOTIDES if b != current]
            chars[i] = random.choice(choices)
    return "".join(chars)


def seed_synthetic_dataset(
    case_count: int = 250,
    reset: bool = False,
    seed: int = 42,
    countries: List[str] = None,
) -> Dict[str, object]:
    random.seed(seed)
    incidence_data, used_fallback = _fetch_latest_incidence()

    if countries:
        requested = [c.strip().lower() for c in countries]
        incidence_data = {
            iso3: d
            for iso3, d in incidence_data.items()
            if iso3.lower() in requested or d["country"].lower() in requested
        }
        if not incidence_data:
            raise ValueError(
                f"No matching countries found. Requested: {countries}. "
                "Use ISO3 codes (e.g. GBR) or full country names."
            )

    sampler = _weighted_country_sampler(incidence_data)

    db = SessionLocal()
    created_cluster_ids = [uuid.uuid4() for _ in range(max(4, case_count // 40))]
    cluster_templates = {
        cid: _build_consensus_sequence(incidence=220.0) for cid in created_cluster_ids
    }

    try:
        if reset:
            db.execute(
                text(
                    "TRUNCATE TABLE case_clusters, tb_interpretation, clusters, "
                    "sample_qc_metrics, consensus_sequences, cases "
                    "RESTART IDENTITY CASCADE"
                )
            )

        for cluster_id in created_cluster_ids:
            db.execute(
                text(
                    "INSERT INTO clusters (cluster_id, snp_distance, investigation_status, alert_flag) "
                    "VALUES (:cluster_id, :snp_distance, :investigation_status, :alert_flag)"
                ),
                {
                    "cluster_id": cluster_id,
                    "snp_distance": random.randint(0, 25),
                    "investigation_status": random.choice(["monitoring", "open", "closed"]),
                    "alert_flag": random.random() < 0.2,
                },
            )

        today = date.today()
        status_weights = [0.78, 0.17, 0.05]

        for i in range(1, case_count + 1):
            iso3 = _pick_country(sampler)
            country = incidence_data[iso3]["country"]
            incidence = incidence_data[iso3]["incidence"]

            case_id = uuid.uuid4()
            specimen_date = today - timedelta(days=random.randint(0, 540))
            case_status = random.choices(
                ["confirmed", "probable", "under_review"], weights=status_weights, k=1
            )[0]
            lineage = _lineage_for_incidence(incidence)
            predicted_resistance, resistance_mutations = _build_resistance_profile(incidence)
            confidence = round(random.uniform(0.76, 0.99), 3)

            case = Case(
                pseudonymised_case_id=case_id,
                local_lab_sample_id=f"LAB-{today.year}-{i:05d}",
                specimen_date=specimen_date,
                geographic_region=country,
                case_status=case_status,
                created_at=datetime.utcnow(),
            )
            db.add(case)
            db.flush()

            db.execute(
                text(
                    "INSERT INTO tb_interpretation (sample_id, species_confirmation, lineage, sublineage, "
                    "resistance_mutations, predicted_drug_resistance, confidence_score, interpretation_summary) "
                    "VALUES (:sample_id, :species_confirmation, :lineage, :sublineage, "
                    "CAST(:resistance_mutations AS jsonb), CAST(:predicted_drug_resistance AS jsonb), "
                    ":confidence_score, :interpretation_summary)"
                ),
                {
                    "sample_id": case_id,
                    "species_confirmation": "M. tuberculosis complex",
                    "lineage": lineage,
                    "sublineage": f"{lineage}.{random.randint(1, 9)}",
                    "resistance_mutations": json.dumps(resistance_mutations),
                    "predicted_drug_resistance": json.dumps(predicted_resistance),
                    "confidence_score": confidence,
                    "interpretation_summary": (
                        f"Synthetic interpretation grounded in public incidence trends for {country}."
                    ),
                },
            )

            assigned_cluster_id = None
            if random.random() < 0.7:
                assigned_cluster_id = random.choice(created_cluster_ids)
                db.execute(
                    text(
                        "INSERT INTO case_clusters (sample_id, cluster_id) VALUES (:sample_id, :cluster_id)"
                    ),
                    {"sample_id": case_id, "cluster_id": assigned_cluster_id},
                )

            # Most synthetic cases include sequence records so sequencing coverage KPI is meaningful.
            sequencing_chance = min(0.95, 0.75 + (incidence / 3000.0))
            if random.random() < sequencing_chance:
                if assigned_cluster_id is not None:
                    # Cluster-linked samples are close variants of a shared template.
                    base = cluster_templates[assigned_cluster_id]
                    sequence = _mutate_sequence(base, mutation_rate=0.008)
                else:
                    # Unclustered samples remain more diverse.
                    base = _build_consensus_sequence(incidence)
                    sequence = _mutate_sequence(base, mutation_rate=0.08)
                db.execute(
                    text(
                        "INSERT INTO consensus_sequences (sample_id, sequence, length) "
                        "VALUES (:sample_id, :sequence, :length)"
                    ),
                    {
                        "sample_id": case_id,
                        "sequence": sequence,
                        "length": len(sequence),
                    },
                )

                # Synthetic QC metrics — realistic distributions for WGS TB samples.
                mean_depth = round(random.uniform(60.0, 280.0), 1)
                coverage_breadth = round(random.uniform(92.0, 99.8), 2)
                ambiguous_pct = round(random.uniform(0.0, 3.5), 2)
                contamination = random.random() < 0.04  # ~4% contamination flag rate
                # QC fail if depth <80, breadth <95, or contamination flagged
                qc_fail = contamination or mean_depth < 80.0 or coverage_breadth < 95.0
                qc_status = "fail" if qc_fail else "pass"
                if qc_fail:
                    reasons = []
                    if contamination:
                        reasons.append("contamination signal detected")
                    if mean_depth < 80.0:
                        reasons.append(f"mean depth {mean_depth}x below threshold 80x")
                    if coverage_breadth < 95.0:
                        reasons.append(f"coverage breadth {coverage_breadth}% below 95%")
                    qc_failure_reason = "; ".join(reasons)
                else:
                    qc_failure_reason = None
                reported_at = datetime.utcnow() - timedelta(
                    days=random.randint(0, max(0, (today - specimen_date).days))
                )
                db.execute(
                    text(
                        "INSERT INTO sample_qc_metrics "
                        "(sample_id, mean_depth, coverage_breadth, ambiguous_base_percent, "
                        "contamination_flag, qc_status, qc_failure_reason, reported_at) "
                        "VALUES (:sample_id, :mean_depth, :coverage_breadth, :ambiguous_base_percent, "
                        ":contamination_flag, :qc_status, :qc_failure_reason, :reported_at)"
                    ),
                    {
                        "sample_id": case_id,
                        "mean_depth": mean_depth,
                        "coverage_breadth": coverage_breadth,
                        "ambiguous_base_percent": ambiguous_pct,
                        "contamination_flag": contamination,
                        "qc_status": qc_status,
                        "qc_failure_reason": qc_failure_reason,
                        "reported_at": reported_at,
                    },
                )

        db.execute(
            text(
                "INSERT INTO audit_log (action, user_id, details, timestamp) "
                "VALUES (:action, :user_id, CAST(:details AS jsonb), NOW())"
            ),
            {
                "action": "seed_synthetic_dataset",
                "user_id": "system",
                "details": json.dumps(
                    {
                        "case_count": case_count,
                        "source": WORLD_BANK_SOURCE_URL,
                        "used_fallback": used_fallback,
                        "countries_filter": countries or "all",
                    }
                ),
            },
        )

        db.commit()
        return {
            "status": "ok",
            "cases_inserted": case_count,
            "clusters_inserted": len(created_cluster_ids),
            "source": WORLD_BANK_SOURCE_URL,
            "used_fallback": used_fallback,
            "countries": sorted([row["country"] for row in incidence_data.values()]),
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed synthetic TB data based on public online incidence")
    parser.add_argument("--cases", type=int, default=250, help="Number of synthetic cases to generate")
    parser.add_argument("--reset", action="store_true", help="Truncate related tables before seeding")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    args = parser.parse_args()

    result = seed_synthetic_dataset(case_count=args.cases, reset=args.reset, seed=args.seed)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
