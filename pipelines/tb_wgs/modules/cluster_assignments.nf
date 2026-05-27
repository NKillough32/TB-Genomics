process CLUSTER_ASSIGNMENTS {
  tag "cluster_assignments"
  container "python:3.11-slim"

  input:
  path snp_matrix

  output:
  path "cluster_assignments.csv", emit: cluster_assignments

  script:
  """
  # Production command placeholder: connected components at params.cluster_threshold.
  python - <<'PY'
  import csv
  with open("cluster_assignments.csv", "w", newline="") as handle:
      writer = csv.DictWriter(handle, fieldnames=["sample_id", "cluster_id", "cluster_method"])
      writer.writeheader()
  PY
  """
}
