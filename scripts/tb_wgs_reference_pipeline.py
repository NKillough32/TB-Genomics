"""Small deterministic TB WGS pipeline reference implementation.

This script backs the in-repository validation workflow. It intentionally keeps
the fixture runner dependency-free so CI can verify the output contract quickly.
Production Snakemake deployments should replace the internals with locked
containerised BWA/Samtools/Bcftools/TB-Profiler commands while retaining the
same output files and manifest fields.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


BASES = {"A", "C", "G", "T"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_fasta(path: Path) -> tuple[str, str]:
    name = ""
    parts: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            if line.startswith(">"):
                name = line[1:].split()[0]
            else:
                parts.append(line.upper())
    if not name or not parts:
        raise ValueError(f"No FASTA sequence found in {path}")
    return name, "".join(parts)


def _read_fastq(path: Path) -> list[tuple[str, str, str]]:
    reads = []
    with path.open(encoding="utf-8") as handle:
        while True:
            header = handle.readline().strip()
            if not header:
                break
            sequence = handle.readline().strip().upper()
            plus = handle.readline().strip()
            quality = handle.readline().strip()
            if not header.startswith("@") or plus != "+":
                raise ValueError(f"Malformed FASTQ record in {path}")
            if len(sequence) != len(quality):
                raise ValueError(f"Sequence/quality length mismatch in {path}")
            reads.append((header[1:], sequence, quality))
    if not reads:
        raise ValueError(f"No reads found in {path}")
    return reads


def _read_sample_sheet(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    required = {"sample_id", "fastq_1"}
    missing = required - set(rows[0].keys() if rows else [])
    if missing:
        raise ValueError(f"Sample sheet missing columns: {sorted(missing)}")
    return rows


def _read_mask_positions(path: Path | None) -> set[int]:
    positions: set[int] = set()
    if not path or not str(path) or not path.exists():
        return positions
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            if not raw.strip() or raw.startswith("#"):
                continue
            chrom, start, end, *_rest = raw.rstrip("\n").split("\t")
            _ = chrom
            for idx in range(int(start), int(end)):
                positions.add(idx)
    return positions


def _best_offset(read: str, reference: str) -> int | None:
    if len(read) > len(reference):
        return None
    best_offset = None
    best_mismatches = None
    for offset in range(0, len(reference) - len(read) + 1):
        mismatches = sum(1 for idx, base in enumerate(read) if base != reference[offset + idx])
        if best_mismatches is None or mismatches < best_mismatches:
            best_mismatches = mismatches
            best_offset = offset
    return best_offset


def _consensus_from_reads(reads: list[tuple[str, str, str]], reference: str) -> tuple[str, list[int], int]:
    counts = [Counter() for _ in reference]
    mapped_reads = 0
    for _name, sequence, _quality in reads:
        offset = _best_offset(sequence, reference)
        if offset is None:
            continue
        mapped_reads += 1
        for idx, base in enumerate(sequence):
            ref_pos = offset + idx
            if ref_pos < len(counts):
                counts[ref_pos][base] += 1
    consensus = []
    depths = []
    for idx, counter in enumerate(counts):
        depth = sum(counter.values())
        depths.append(depth)
        if not depth:
            consensus.append("N")
            continue
        base, _count = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[0]
        consensus.append(base if base in BASES else "N")
    return "".join(consensus), depths, mapped_reads


def _mask_sequence(sequence: str, mask_positions: set[int]) -> str:
    chars = list(sequence)
    for idx in mask_positions:
        if 0 <= idx < len(chars):
            chars[idx] = "N"
    return "".join(chars)


def _distance(left: str, right: str) -> int:
    return sum(1 for a, b in zip(left, right) if a in BASES and b in BASES and a != b)


def _lineage_call(consensus: str) -> str:
    # Fixture rule: a variant at 0-based position 16 marks the validation lineage.
    return "L4.1-fixture" if len(consensus) > 16 and consensus[16] == "T" else "L4-fixture"


def _resistance_calls(sample_id: str, consensus: str) -> list[dict[str, str]]:
    calls = []
    # Fixture rule: a variant at 0-based position 27 represents an rpoB marker.
    if len(consensus) > 27 and consensus[27] == "A":
        calls.append(
            {
                "sample_id": sample_id,
                "drug": "Rifampicin",
                "gene": "rpoB",
                "mutation": "fixture_pos28_A",
                "prediction": "resistant",
                "confidence": "1.0",
                "source_tool": "fixture-resistance-caller",
            }
        )
    return calls


def _cluster_assignments(masked_sequences: dict[str, str], threshold: int) -> list[dict[str, str]]:
    sample_ids = list(masked_sequences.keys())
    parent = {sample_id: sample_id for sample_id in sample_ids}

    def find(sample_id: str) -> str:
        if parent[sample_id] != sample_id:
            parent[sample_id] = find(parent[sample_id])
        return parent[sample_id]

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left_idx, left in enumerate(sample_ids):
        for right in sample_ids[left_idx + 1 :]:
            if _distance(masked_sequences[left], masked_sequences[right]) <= threshold:
                union(left, right)

    members_by_root: dict[str, list[str]] = {}
    for sample_id in sample_ids:
        members_by_root.setdefault(find(sample_id), []).append(sample_id)

    rows = []
    cluster_idx = 1
    for members in members_by_root.values():
        if len(members) < 2:
            rows.append({"sample_id": members[0], "cluster_id": "", "cluster_method": "snp_threshold"})
            continue
        cluster_id = f"VAL-CLUSTER-{cluster_idx:03d}"
        cluster_idx += 1
        for sample_id in sorted(members):
            rows.append({"sample_id": sample_id, "cluster_id": cluster_id, "cluster_method": "snp_threshold"})
    return sorted(rows, key=lambda row: row["sample_id"])


def run_pipeline(args: argparse.Namespace) -> dict:
    sample_sheet = Path(args.sample_sheet)
    reference_path = Path(args.reference)
    mask_bed = Path(args.mask_bed) if args.mask_bed else None
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    reference_name, reference = _read_fasta(reference_path)
    mask_positions = _read_mask_positions(mask_bed)
    samples = _read_sample_sheet(sample_sheet)

    qc_rows = []
    mapping_rows = []
    variant_rows = []
    masked_sequences: dict[str, str] = {}
    lineage_rows = []
    resistance_rows = []
    input_files = [sample_sheet, reference_path]
    if mask_bed:
        input_files.append(mask_bed)

    for sample in samples:
        sample_id = sample["sample_id"].strip()
        fastq_paths = [Path(sample["fastq_1"])]
        if sample.get("fastq_2"):
            fastq_paths.append(Path(sample["fastq_2"]))
        input_files.extend(fastq_paths)
        reads = []
        for fastq_path in fastq_paths:
            reads.extend(_read_fastq(fastq_path))
        total_bases = sum(len(sequence) for _name, sequence, _quality in reads)
        ambiguous_bases = sum(
            1 for _name, sequence, _quality in reads for base in sequence if base not in BASES
        )
        ambiguous_percent = round((ambiguous_bases / total_bases) * 100.0, 4) if total_bases else 100.0
        consensus, depths, mapped_reads = _consensus_from_reads(reads, reference)
        mean_depth = round(sum(depths) / len(depths), 4) if depths else 0.0
        coverage_breadth = round(sum(1 for depth in depths if depth > 0) / len(depths), 4) if depths else 0.0
        qc_status = (
            "pass"
            if len(reads) >= args.min_reads
            and total_bases >= args.min_bases
            and ambiguous_percent <= args.max_ambiguous_percent
            and mean_depth >= args.min_mean_depth
            else "fail"
        )
        failure_reasons = []
        if len(reads) < args.min_reads:
            failure_reasons.append("read_count_below_minimum")
        if total_bases < args.min_bases:
            failure_reasons.append("base_count_below_minimum")
        if ambiguous_percent > args.max_ambiguous_percent:
            failure_reasons.append("ambiguous_percent_above_maximum")
        if mean_depth < args.min_mean_depth:
            failure_reasons.append("mean_depth_below_minimum")

        qc_rows.append(
            {
                "sample_id": sample_id,
                "read_count": len(reads),
                "total_bases": total_bases,
                "mean_depth": mean_depth,
                "coverage_breadth": coverage_breadth,
                "ambiguous_base_percent": ambiguous_percent,
                "contamination_flag": "false",
                "qc_status": qc_status,
                "qc_failure_reason": ";".join(failure_reasons),
            }
        )
        mapping_rows.append(
            {
                "sample_id": sample_id,
                "reference": reference_name,
                "read_count": len(reads),
                "mapped_reads": mapped_reads,
                "mapped_percent": round((mapped_reads / len(reads)) * 100.0, 4) if reads else 0.0,
            }
        )
        for pos, (ref_base, sample_base) in enumerate(zip(reference, consensus), start=1):
            if ref_base in BASES and sample_base in BASES and ref_base != sample_base:
                variant_rows.append(
                    {
                        "sample_id": sample_id,
                        "chrom": reference_name,
                        "pos": pos,
                        "ref": ref_base,
                        "alt": sample_base,
                    }
                )
        masked = _mask_sequence(consensus, mask_positions)
        masked_sequences[sample_id] = masked
        lineage_rows.append(
            {
                "sample_id": sample_id,
                "lineage": _lineage_call(consensus),
                "sublineage": _lineage_call(consensus),
                "source_tool": "fixture-lineage-caller",
            }
        )
        resistance_rows.extend(_resistance_calls(sample_id, consensus))

    _write_csv(outdir / "sample_qc_metrics.csv", qc_rows)
    _write_csv(outdir / "mapping_summary.csv", mapping_rows)
    _write_vcf_gz(outdir / "variants.vcf.gz", variant_rows)
    _write_fasta(outdir / "masked_alignment.fasta", masked_sequences)
    _write_distance_matrix(outdir / "snp_distance_matrix.tsv", masked_sequences)
    _write_csv(outdir / "lineage_calls.csv", lineage_rows)
    _write_csv(
        outdir / "resistance_calls.csv",
        resistance_rows,
        fieldnames=["sample_id", "drug", "gene", "mutation", "prediction", "confidence", "source_tool"],
    )
    _write_csv(
        outdir / "cluster_assignments.csv",
        _cluster_assignments(masked_sequences, args.cluster_threshold),
        fieldnames=["sample_id", "cluster_id", "cluster_method"],
    )

    output_files = [
        outdir / "sample_qc_metrics.csv",
        outdir / "mapping_summary.csv",
        outdir / "variants.vcf.gz",
        outdir / "masked_alignment.fasta",
        outdir / "snp_distance_matrix.tsv",
        outdir / "lineage_calls.csv",
        outdir / "resistance_calls.csv",
        outdir / "cluster_assignments.csv",
    ]
    manifest = {
        "pipeline_name": "tb_wgs_reference_validation",
        "pipeline_version": "0.1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workflow_engine": "snakemake",
        "reference": "H37Rv NC_000962.3",
        "reference_path": str(reference_path),
        "mask_bed": str(mask_bed) if mask_bed else None,
        "mask_file_sha256": _sha256(mask_bed) if mask_bed else None,
        "sample_sheet": str(sample_sheet),
        "sample_count": len(samples),
        "tb_profiler_version": "fixture",
        "tb_profiler_db_version": "fixture",
        "snp_dists_version": "fixture",
        "steps": [
            "FASTQ",
            "QC",
            "mapping",
            "variant_calling",
            "masking",
            "masked_FASTA",
            "SNP_distance_matrix",
            "lineage",
            "resistance_calls",
            "pipeline_manifest",
        ],
        "parameters": {
            "min_reads": args.min_reads,
            "min_bases": args.min_bases,
            "max_ambiguous_percent": args.max_ambiguous_percent,
            "min_mean_depth": args.min_mean_depth,
            "cluster_threshold": args.cluster_threshold,
        },
        "input_hashes": {str(path): _sha256(path) for path in input_files},
        "output_hashes": {path.name: _sha256(path) for path in output_files},
    }
    manifest_path = outdir / "pipeline_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_vcf_gz(path: Path, rows: list[dict]) -> None:
    with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as raw_handle:
        handle = raw_handle
        lines = ["##fileformat=VCFv4.2\n", "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tSAMPLE\n"]
        for row in rows:
            lines.append(
                f"{row['chrom']}\t{row['pos']}\t.\t{row['ref']}\t{row['alt']}\t.\tPASS\t.\t{row['sample_id']}\n"
            )
        handle.write("".join(lines).encode("utf-8"))


def _write_fasta(path: Path, sequences: dict[str, str]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for sample_id, sequence in sequences.items():
            handle.write(f">{sample_id}\n{sequence}\n")


def _write_distance_matrix(path: Path, sequences: dict[str, str]) -> None:
    sample_ids = list(sequences.keys())
    with path.open("w", encoding="utf-8") as handle:
        handle.write("sample_id\t" + "\t".join(sample_ids) + "\n")
        for left in sample_ids:
            distances = [_distance(sequences[left], sequences[right]) for right in sample_ids]
            handle.write(left + "\t" + "\t".join(str(value) for value in distances) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-sheet", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--mask-bed", default="")
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--min-reads", type=int, default=1)
    parser.add_argument("--min-bases", type=int, default=20)
    parser.add_argument("--max-ambiguous-percent", type=float, default=5.0)
    parser.add_argument("--min-mean-depth", type=float, default=1.0)
    parser.add_argument("--cluster-threshold", type=int, default=12)
    return parser.parse_args()


def main() -> None:
    manifest = run_pipeline(parse_args())
    print(json.dumps({"status": "ok", "sample_count": manifest["sample_count"]}, indent=2))


if __name__ == "__main__":
    main()
