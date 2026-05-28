import argparse
import gzip
from pathlib import Path


def merge_csvs(inputs: list[Path], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    wrote_header = False
    with output.open("w", encoding="utf-8", newline="") as out:
        for path in sorted(inputs):
            lines = path.read_text(encoding="utf-8").splitlines()
            if not lines:
                continue
            if not wrote_header:
                out.write(lines[0] + "\n")
                wrote_header = True
            for line in lines[1:]:
                out.write(line + "\n")


def merge_vcfs(inputs: list[Path], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as fileobj:
        with gzip.GzipFile(filename="", fileobj=fileobj, mode="wb", mtime=0) as raw:
            raw.write(b"##fileformat=VCFv4.2\n")
            raw.write(b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            for path in sorted(inputs):
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    for line in handle:
                        if not line.startswith("#"):
                            raw.write(line.encode("utf-8"))


def aggregate_sample_outputs(input_dir: Path, output_dir: Path) -> None:
    merge_csvs(list(input_dir.glob("*.sample_qc_metrics.csv")), output_dir / "sample_qc_metrics.csv")
    merge_csvs(list(input_dir.glob("*.mapping_summary.csv")), output_dir / "mapping_summary.csv")
    merge_csvs(list(input_dir.glob("*.lineage_calls.csv")), output_dir / "lineage_calls.csv")
    merge_csvs(list(input_dir.glob("*.resistance_calls.csv")), output_dir / "resistance_calls.csv")
    merge_vcfs(list(input_dir.glob("*.variants.vcf.gz")), output_dir / "variants.vcf.gz")


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate per-sample TB WGS pipeline outputs.")
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    aggregate_sample_outputs(args.input_dir, args.output_dir)


if __name__ == "__main__":
    main()
