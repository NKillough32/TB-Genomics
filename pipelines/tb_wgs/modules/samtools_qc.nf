process SAMTOOLS_QC {
  tag "samtools_qc"
  container "quay.io/biocontainers/samtools:1.21--h50ea8bc_0"

  input:
  path bam

  output:
  path "sample_qc_metrics.csv", emit: sample_qc

  script:
  """
  # Production command placeholder:
  # samtools depth/stats -> sample_qc_metrics.csv
  echo "sample_id,mean_depth,coverage_breadth,ambiguous_base_percent,contamination_flag,qc_status,qc_failure_reason" > sample_qc_metrics.csv
  """
}
