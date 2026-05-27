"""Run the TB WGS validation harness and compare reportable outputs."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.tb_wgs_reference_pipeline import run_pipeline
from scripts.validate_tb_wgs_outputs import validate_outputs


REPORTABLE_COMPARISONS = (
    "sample_qc_metrics.csv",
    "snp_distance_matrix.tsv",
    "lineage_calls.csv",
    "resistance_calls.csv",
    "cluster_assignments.csv",
)


class PipelineArgs:
    def __init__(self, test_data: Path, observed: Path):
        self.sample_sheet = str(test_data / "samples.csv")
        self.reference = str(test_data / "reference" / "ref.fasta")
        self.mask_bed = str(test_data / "reference" / "mask.bed")
        self.outdir = str(observed)
        self.min_reads = 1
        self.min_bases = 20
        self.max_ambiguous_percent = 5.0
        self.min_mean_depth = 1.0
        self.cluster_threshold = 12


def _read_table(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _normalised_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _compare_reportable_outputs(observed: Path, expected: Path) -> list[str]:
    failures = []
    for filename in REPORTABLE_COMPARISONS:
        observed_path = observed / filename
        expected_path = expected / filename
        if not observed_path.exists():
            failures.append(f"missing observed reportable output: {filename}")
            continue
        if not expected_path.exists():
            failures.append(f"missing expected reportable output: {filename}")
            continue
        if _normalised_text(observed_path) != _normalised_text(expected_path):
            failures.append(f"reportable output differs: {filename}")
    return failures


def _summarise(observed: Path, failures: list[str]) -> dict:
    qc_rows = _read_table(observed / "sample_qc_metrics.csv") if (observed / "sample_qc_metrics.csv").exists() else []
    lineage_rows = _read_table(observed / "lineage_calls.csv") if (observed / "lineage_calls.csv").exists() else []
    resistance_rows = _read_table(observed / "resistance_calls.csv") if (observed / "resistance_calls.csv").exists() else []
    cluster_rows = _read_table(observed / "cluster_assignments.csv") if (observed / "cluster_assignments.csv").exists() else []
    return {
        "status": "pass" if not failures else "fail",
        "failure_count": len(failures),
        "failures": failures,
        "qc_pass": sum(1 for row in qc_rows if row.get("qc_status") == "pass"),
        "qc_fail": sum(1 for row in qc_rows if row.get("qc_status") == "fail"),
        "lineage_calls": len(lineage_rows),
        "resistance_calls": len(resistance_rows),
        "clustered_samples": sum(1 for row in cluster_rows if row.get("cluster_id")),
    }


def run_validation(test_data: Path, expected: Path, observed: Path, clean: bool = True) -> dict:
    if clean and observed.exists():
        shutil.rmtree(observed)
    observed.mkdir(parents=True, exist_ok=True)
    run_pipeline(PipelineArgs(test_data, observed))
    failures = []
    failures.extend(validate_outputs(observed, expected))
    failures.extend(_compare_reportable_outputs(observed, expected))
    report = _summarise(observed, failures)
    (observed / "validation_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-data", default="validation/test_data/tb_wgs")
    parser.add_argument("--expected", default="validation/expected_outputs/tb_wgs")
    parser.add_argument("--observed", default="validation/observed_outputs/tb_wgs")
    parser.add_argument("--keep-observed", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = run_validation(
        Path(args.test_data),
        Path(args.expected),
        Path(args.observed),
        clean=not args.keep_observed,
    )
    print(json.dumps(report, indent=2))
    if report["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
