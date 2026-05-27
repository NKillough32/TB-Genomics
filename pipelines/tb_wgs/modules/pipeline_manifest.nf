process PIPELINE_MANIFEST {
  tag "pipeline_manifest"
  container "python:3.11-slim"

  input:
  path sample_qc
  path variants
  path masked_alignment
  path snp_matrix
  path lineage
  path resistance

  output:
  path "pipeline_manifest.json", emit: manifest

  script:
  """
  python - <<'PY'
  import hashlib, json, pathlib
  files = [
      "sample_qc_metrics.csv",
      "variants.vcf.gz",
      "masked_alignment.fasta",
      "snp_distance_matrix.tsv",
      "lineage_calls.csv",
      "resistance_calls.csv",
  ]
  def sha256(path):
      h = hashlib.sha256()
      with open(path, "rb") as handle:
          for chunk in iter(lambda: handle.read(1024 * 1024), b""):
              h.update(chunk)
      return h.hexdigest()
  pathlib.Path("pipeline_manifest.json").write_text(json.dumps({
      "pipeline_version": "${params.pipeline_version}",
      "reference": "H37Rv NC_000962.3",
      "mask_file_sha256": None,
      "tb_profiler_version": "container",
      "tb_profiler_db_version": "container",
      "snp_dists_version": "container",
      "input_hashes": {},
      "output_hashes": {name: sha256(name) for name in files if pathlib.Path(name).exists()}
  }, indent=2) + "\\n")
  PY
  """
}
