"""Validate TB WGS pipeline outputs against an expected fixture directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_OUTPUTS = (
    "sample_qc_metrics.csv",
    "masked_alignment.fasta",
    "snp_distance_matrix.tsv",
    "lineage_calls.csv",
    "resistance_calls.csv",
    "pipeline_manifest.json",
)


def _normalise_manifest(text: str) -> dict:
    manifest = json.loads(text)
    # Runtime timestamps and hashes are checked structurally in tests, but exact
    # fixture comparison ignores them so the manifest remains reproducible across
    # checkout paths and line-ending modes.
    manifest.pop("generated_at", None)
    manifest.pop("inputs", None)
    manifest.pop("outputs", None)
    manifest["reference"] = Path(str(manifest["reference"]).replace("\\", "/")).name
    manifest["sample_sheet"] = Path(str(manifest["sample_sheet"]).replace("\\", "/")).name
    if manifest.get("mask_bed"):
        manifest["mask_bed"] = Path(str(manifest["mask_bed"]).replace("\\", "/")).name
    return manifest


def _normalise(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if path.name == "pipeline_manifest.json":
        return json.dumps(_normalise_manifest(text), indent=2, sort_keys=True) + "\n"
    return text


def validate_outputs(observed: Path, expected: Path) -> list[str]:
    failures = []
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
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        missing_manifest_outputs = [
            name for name in REQUIRED_OUTPUTS if name != "pipeline_manifest.json" and name not in manifest.get("outputs", {})
        ]
        if missing_manifest_outputs:
            failures.append(f"manifest missing output hashes: {missing_manifest_outputs}")
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
