process CLUSTER_ASSIGNMENTS {
  tag "cluster_assignments"
  container "python:3.11-slim"

  input:
  path snp_matrix
  val cluster_threshold

  output:
  path "cluster_assignments.csv", emit: cluster_assignments

  script:
  """
  # Production command placeholder: connected components at the configured SNP threshold.
  python - <<'PY'
  import csv
  threshold = int("${cluster_threshold}")
  with open("cluster_assignments.csv", "w", newline="") as handle:
      writer = csv.DictWriter(handle, fieldnames=["sample_id", "cluster_id", "cluster_method"])
      writer.writeheader()
  assert threshold >= 0
  PY
  """
}
