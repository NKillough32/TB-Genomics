process AGGREGATE_SAMPLE_OUTPUTS {
  tag "aggregate_sample_outputs"
  container "python:3.11-slim"

  input:
  path sample_qc_files
  path mapping_summary_files
  path variant_files
  path lineage_files
  path resistance_files

  output:
  path "sample_qc_metrics.csv", emit: sample_qc
  path "mapping_summary.csv", emit: mapping_summary
  path "variants.vcf.gz", emit: variants
  path "lineage_calls.csv", emit: lineage
  path "resistance_calls.csv", emit: resistance

  script:
  """
  python - <<'PY'
  import gzip, pathlib

  def merge_csv(pattern, output):
      files = sorted(pathlib.Path(".").glob(pattern))
      with open(output, "w", encoding="utf-8", newline="") as out:
          wrote_header = False
          for path in files:
              lines = path.read_text(encoding="utf-8").splitlines()
              if not lines:
                  continue
              if not wrote_header:
                  out.write(lines[0] + "\\n")
                  wrote_header = True
              for line in lines[1:]:
                  out.write(line + "\\n")

  merge_csv("*.sample_qc_metrics.csv", "sample_qc_metrics.csv")
  merge_csv("*.mapping_summary.csv", "mapping_summary.csv")
  merge_csv("*.lineage_calls.csv", "lineage_calls.csv")
  merge_csv("*.resistance_calls.csv", "resistance_calls.csv")

  # Placeholder only: production should use bcftools concat/merge so contig,
  # INFO, FILTER, FORMAT, and bcftools provenance headers are preserved.
  with gzip.open("variants.vcf.gz", "wt") as out:
      out.write("##fileformat=VCFv4.2\\n#CHROM\\tPOS\\tID\\tREF\\tALT\\tQUAL\\tFILTER\\tINFO\\n")
      for path in sorted(pathlib.Path(".").glob("*.variants.vcf.gz")):
          with gzip.open(path, "rt") as handle:
              for line in handle:
                  if not line.startswith("#"):
                      out.write(line)
  PY
  """
}
