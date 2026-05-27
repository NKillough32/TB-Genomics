"""Validate TB WGS pipeline outputs against an expected fixture directory."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from pathlib import Path


REQUIRED_OUTPUTS = (
    "sample_qc_metrics.csv",
    "variants.vcf.gz",
    "masked_alignment.fasta",
    "snp_distance_matrix.tsv",
    "lineage_calls.csv",
    "resistance_calls.csv",
    "pipeline_manifest.json",
)

SCHEMA_DIR = Path("validation/tb_wgs/schemas")


def _normalise_manifest(text: str) -> dict:
    manifest = json.loads(text)
    # Runtime timestamps and hashes are checked structurally in tests, but exact
    # fixture comparison ignores them so the manifest remains reproducible across
    # checkout paths and line-ending modes.
    manifest.pop("generated_at", None)
    manifest.pop("input_hashes", None)
    manifest.pop("output_hashes", None)
    manifest["reference"] = Path(str(manifest["reference"]).replace("\\", "/")).name
    if manifest.get("reference_path"):
        manifest["reference_path"] = Path(str(manifest["reference_path"]).replace("\\", "/")).name
    manifest["sample_sheet"] = Path(str(manifest["sample_sheet"]).replace("\\", "/")).name
    if manifest.get("mask_bed"):
        manifest["mask_bed"] = Path(str(manifest["mask_bed"]).replace("\\", "/")).name
    return manifest


def _normalise(path: Path) -> str:
    if path.suffix == ".gz":
        text = gzip.open(path, "rt", encoding="utf-8").read().replace("\r\n", "\n")
        return text
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if path.name == "pipeline_manifest.json":
        return json.dumps(_normalise_manifest(text), indent=2, sort_keys=True) + "\n"
    return text


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _schema(name: str, schema_dir: Path) -> dict:
    path = schema_dir / name
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_table(path: Path, schema: dict, failures: list[str]) -> None:
    rows = _read_csv(path)
    required = schema.get("required_columns", [])
    actual = list(rows[0].keys()) if rows else []
    missing = [col for col in required if col not in actual]
    if missing:
        failures.append(f"{path.name} missing required columns: {missing}")
        return
    numeric_columns = schema.get("numeric_columns", [])
    enum_columns = schema.get("enum_columns", {})
    for idx, row in enumerate(rows, start=2):
        for column in required:
            if column != "qc_failure_reason" and row.get(column) is None:
                failures.append(f"{path.name}:{idx} missing value for {column}")
        for column in numeric_columns:
            value = row.get(column, "")
            try:
                float(value)
            except ValueError:
                failures.append(f"{path.name}:{idx} non-numeric {column}: {value}")
        for column, allowed in enum_columns.items():
            value = str(row.get(column, "")).strip().lower()
            if value and value not in allowed:
                failures.append(f"{path.name}:{idx} invalid {column}: {value}")


def _read_fasta_ids(path: Path) -> list[str]:
    ids = []
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            if raw.startswith(">"):
                ids.append(raw[1:].strip().split()[0])
    return ids


def _validate_distance_matrix(path: Path, fasta_ids: list[str], failures: list[str]) -> None:
    lines = [line.rstrip("\n").split("\t") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        failures.append("snp_distance_matrix.tsv is empty")
        return
    header = lines[0]
    if not header or header[0] != "sample_id":
        failures.append("snp_distance_matrix.tsv first header must be sample_id")
        return
    sample_ids = header[1:]
    if sample_ids != fasta_ids:
        failures.append("snp_distance_matrix.tsv sample order does not match masked_alignment.fasta")
    matrix: dict[str, dict[str, int]] = {}
    for row in lines[1:]:
        if len(row) != len(header):
            failures.append(f"snp_distance_matrix.tsv row has wrong width: {row[0] if row else '<empty>'}")
            continue
        sample_id = row[0]
        matrix[sample_id] = {}
        for other_id, value in zip(sample_ids, row[1:]):
            try:
                matrix[sample_id][other_id] = int(value)
            except ValueError:
                failures.append(f"snp_distance_matrix.tsv non-integer distance {sample_id}-{other_id}: {value}")
    for sample_id in sample_ids:
        if matrix.get(sample_id, {}).get(sample_id) != 0:
            failures.append(f"snp_distance_matrix.tsv diagonal is not zero for {sample_id}")
        for other_id in sample_ids:
            left = matrix.get(sample_id, {}).get(other_id)
            right = matrix.get(other_id, {}).get(sample_id)
            if left != right:
                failures.append(f"snp_distance_matrix.tsv is not symmetric for {sample_id}-{other_id}")


def _validate_manifest(path: Path, schema: dict, failures: list[str]) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    missing = [field for field in schema.get("required_fields", []) if field not in manifest]
    if missing:
        failures.append(f"pipeline_manifest.json missing required fields: {missing}")
    steps = set(manifest.get("steps", []))
    missing_steps = [step for step in schema.get("required_steps", []) if step not in steps]
    if missing_steps:
        failures.append(f"pipeline_manifest.json missing required steps: {missing_steps}")
    outputs = manifest.get("output_hashes", {})
    missing_outputs = [name for name in schema.get("required_outputs", []) if name not in outputs]
    if missing_outputs:
        failures.append(f"pipeline_manifest.json missing required output hashes: {missing_outputs}")


def validate_contract(observed: Path, schema_dir: Path = SCHEMA_DIR) -> list[str]:
    failures: list[str] = []
    for filename in REQUIRED_OUTPUTS:
        if not (observed / filename).exists():
            failures.append(f"missing required output: {filename}")
    if failures:
        return failures
    _validate_table(
        observed / "sample_qc_metrics.csv",
        _schema("sample_qc_metrics.schema.json", schema_dir),
        failures,
    )
    _validate_table(
        observed / "lineage_calls.csv",
        _schema("lineage_calls.schema.json", schema_dir),
        failures,
    )
    _validate_table(
        observed / "resistance_calls.csv",
        _schema("resistance_calls.schema.json", schema_dir),
        failures,
    )
    fasta_ids = _read_fasta_ids(observed / "masked_alignment.fasta")
    if not fasta_ids:
        failures.append("masked_alignment.fasta contains no sample records")
    _validate_distance_matrix(observed / "snp_distance_matrix.tsv", fasta_ids, failures)
    _validate_manifest(
        observed / "pipeline_manifest.json",
        _schema("pipeline_manifest.schema.json", schema_dir),
        failures,
    )
    return failures


def validate_outputs(observed: Path, expected: Path) -> list[str]:
    failures = validate_contract(observed)
    for filename in REQUIRED_OUTPUTS:
        observed_path = observed / filename
        expected_path = expected / filename
        if not observed_path.exists():
            failures.append(f"missing observed output: {filename}")
            continue
        if not expected_path.exists():
            failures.append(f"missing expected output: {filename}")
            continue
        if _normalise(observed_path) != _normalise(expected_path):
            failures.append(f"output differs from expected fixture: {filename}")
    manifest_path = observed / "pipeline_manifest.json"
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observed", required=True)
    parser.add_argument("--expected", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    failures = validate_outputs(Path(args.observed), Path(args.expected))
    if failures:
        raise SystemExit("\n".join(failures))
    print("TB WGS output validation passed")


if __name__ == "__main__":
    main()
