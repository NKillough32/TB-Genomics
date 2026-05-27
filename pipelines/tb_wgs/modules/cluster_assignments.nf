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
  python - <<'PY'
  import csv
  threshold = int("${cluster_threshold}")
  with open("${snp_matrix}", newline="", encoding="utf-8") as handle:
      reader = csv.reader(handle, delimiter="\\t")
      header = next(reader)
      samples = header[1:]
      distances = {}
      for row in reader:
          source = row[0]
          for target, value in zip(samples, row[1:]):
              if value != "":
                  distances[(source, target)] = int(float(value))

  parent = {sample: sample for sample in samples}

  def find(sample):
      while parent[sample] != sample:
          parent[sample] = parent[parent[sample]]
          sample = parent[sample]
      return sample

  def union(left, right):
      left_root = find(left)
      right_root = find(right)
      if left_root != right_root:
          parent[right_root] = left_root

  for left in samples:
      for right in samples:
          if left != right and distances.get((left, right), threshold + 1) <= threshold:
              union(left, right)

  clusters = {}
  for sample in sorted(samples):
      root = find(sample)
      clusters.setdefault(root, f"cluster_{len(clusters) + 1}")

  with open("cluster_assignments.csv", "w", newline="", encoding="utf-8") as handle:
      writer = csv.DictWriter(handle, fieldnames=["sample_id", "cluster_id", "cluster_method"])
      writer.writeheader()
      for sample in sorted(samples):
          writer.writerow({
              "sample_id": sample,
              "cluster_id": clusters[find(sample)],
              "cluster_method": "snp_threshold",
          })
  PY
  """
}
