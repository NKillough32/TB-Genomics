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
  # Production command placeholder:
  # samtools depth/stats -> sample_qc_metrics.csv
  echo "sample_id,mean_depth,coverage_breadth,ambiguous_base_percent,contamination_flag,qc_status,qc_failure_reason" > ${sample_id}.sample_qc_metrics.csv
  echo "sample_id,reference,read_count,mapped_reads,mapped_percent" > ${sample_id}.mapping_summary.csv
  """
}
