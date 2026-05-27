process SAMTOOLS_QC {
  tag "${sample_id}"
  container "quay.io/biocontainers/samtools:1.21--h50ea8bc_0"

  input:
  tuple val(sample_id), path(bam), path(bai)

  output:
  tuple val(sample_id), path("${sample_id}.sample_qc_metrics.csv"), emit: sample_qc
  tuple val(sample_id), path("${sample_id}.mapping_summary.csv"), emit: mapping_summary

  script:
  """
  samtools stats "${bam}" > "${sample_id}.samtools.stats.txt"
  samtools depth -a "${bam}" > "${sample_id}.depth.tsv"

  python - <<'PY'
  import csv
  from pathlib import Path

  sample_id = "${sample_id}"
  read_count = 0
  mapped_reads = 0
  total_bases = 0
  reference = "unknown"
  for line in Path(f"{sample_id}.samtools.stats.txt").read_text(encoding="utf-8").splitlines():
      if line.startswith("SN\\traw total sequences:"):
          read_count = int(line.rsplit("\\t", 1)[1])
      elif line.startswith("SN\\treads mapped:"):
          mapped_reads = int(line.rsplit("\\t", 1)[1])
      elif line.startswith("SN\\ttotal length:"):
          total_bases = int(line.rsplit("\\t", 1)[1])

  depths = []
  contigs = set()
  for line in Path(f"{sample_id}.depth.tsv").read_text(encoding="utf-8").splitlines():
      chrom, _pos, depth = line.split("\\t")
      contigs.add(chrom)
      depths.append(int(depth))
  if contigs:
      reference = ";".join(sorted(contigs))
  mean_depth = round(sum(depths) / len(depths), 3) if depths else 0.0
  coverage_breadth = round((sum(1 for depth in depths if depth > 0) / len(depths)) * 100, 3) if depths else 0.0
  mapped_percent = round((mapped_reads / read_count) * 100, 3) if read_count else 0.0
  qc_failures = []
  if read_count < ${params.min_reads}:
      qc_failures.append("read_count_below_minimum")
  if total_bases < ${params.min_bases}:
      qc_failures.append("total_bases_below_minimum")
  if mean_depth < ${params.min_mean_depth}:
      qc_failures.append("mean_depth_below_minimum")
  qc_status = "fail" if qc_failures else "pass"

  with open(f"{sample_id}.sample_qc_metrics.csv", "w", newline="", encoding="utf-8") as handle:
      writer = csv.DictWriter(handle, fieldnames=[
          "sample_id",
          "read_count",
          "total_bases",
          "mean_depth",
          "coverage_breadth",
          "ambiguous_base_percent",
          "contamination_flag",
          "qc_status",
          "qc_failure_reason",
      ])
      writer.writeheader()
      writer.writerow({
          "sample_id": sample_id,
          "read_count": read_count,
          "total_bases": total_bases,
          "mean_depth": mean_depth,
          "coverage_breadth": coverage_breadth,
          "ambiguous_base_percent": 0.0,
          "contamination_flag": "false",
          "qc_status": qc_status,
          "qc_failure_reason": ";".join(qc_failures),
      })

  with open(f"{sample_id}.mapping_summary.csv", "w", newline="", encoding="utf-8") as handle:
      writer = csv.DictWriter(handle, fieldnames=["sample_id", "reference", "read_count", "mapped_reads", "mapped_percent"])
      writer.writeheader()
      writer.writerow({
          "sample_id": sample_id,
          "reference": reference,
          "read_count": read_count,
          "mapped_reads": mapped_reads,
          "mapped_percent": mapped_percent,
      })
  PY
  """
}
