process PIPELINE_MANIFEST {
  tag "pipeline_manifest"
  container "python:3.11-slim"

  input:
  path sample_qc
  path mapping_summary
  path variants
  path masked_alignment
  path snp_matrix
  path cluster_assignments
  path lineage
  path resistance

  output:
  path "pipeline_manifest.json", emit: manifest

  script:
  """
  python - <<'PY'
  import csv, hashlib, json, pathlib
  from datetime import datetime, timezone

  files = [
      "sample_qc_metrics.csv",
      "mapping_summary.csv",
      "variants.vcf.gz",
      "masked_alignment.fasta",
      "snp_distance_matrix.tsv",
      "cluster_assignments.csv",
      "lineage_calls.csv",
      "resistance_calls.csv",
  ]

  def sha256(path):
      h = hashlib.sha256()
      with open(path, "rb") as handle:
          for chunk in iter(lambda: handle.read(1024 * 1024), b""):
              h.update(chunk)
      return h.hexdigest()

  def optional_sha256(path):
      candidate = pathlib.Path(path)
      return sha256(candidate) if candidate.exists() else None

  def count_samples(path):
      candidate = pathlib.Path(path)
      if not candidate.exists():
          return 0
      with candidate.open(newline="", encoding="utf-8") as handle:
          return sum(1 for _ in csv.DictReader(handle))

  input_paths = {
      "${params.sample_sheet}": optional_sha256("${params.sample_sheet}"),
      "${params.reference}": optional_sha256("${params.reference}"),
      "${params.mask_bed}": optional_sha256("${params.mask_bed}"),
  }
  input_hashes = {path: digest for path, digest in input_paths.items() if digest}

  pathlib.Path("pipeline_manifest.json").write_text(json.dumps({
      "pipeline_name": "tb_wgs_nextflow",
      "pipeline_version": "${params.pipeline_version}",
      "generated_at": datetime.now(timezone.utc).isoformat(),
      "workflow_engine": "nextflow",
      "reference": "H37Rv NC_000962.3",
      "reference_path": "${params.reference}",
      "mask_bed": "${params.mask_bed}",
      "mask_file_sha256": optional_sha256("${params.mask_bed}"),
      "sample_sheet": "${params.sample_sheet}",
      "sample_count": count_samples("sample_qc_metrics.csv"),
      "tb_profiler_version": "container",
      "tb_profiler_db_version": "container",
      "snp_dists_version": "container",
      "steps": [
          "FASTQ",
          "QC",
          "mapping",
          "variant_calling",
          "masking",
          "masked_FASTA",
          "SNP_distance_matrix",
          "cluster_assignment",
          "lineage",
          "resistance_calls",
          "pipeline_manifest",
      ],
      "parameters": {
          "min_reads": ${params.min_reads},
          "min_bases": ${params.min_bases},
          "max_ambiguous_percent": ${params.max_ambiguous_percent},
          "min_mean_depth": ${params.min_mean_depth},
          "cluster_threshold": ${params.cluster_threshold},
      },
      "input_hashes": input_hashes,
      "output_hashes": {name: sha256(name) for name in files if pathlib.Path(name).exists()}
  }, indent=2) + "\\n")
  PY
  """
}
