process SNP_DISTS {
  tag "snp_dists"
  container "quay.io/biocontainers/snp-dists:0.8.2--h7b50bb2_2"

  input:
  path masked_alignment

  output:
  path "snp_distance_matrix.tsv", emit: snp_matrix

  script:
  """
  snp-dists "${masked_alignment}" > snp_distance_matrix.tsv
  """
}
