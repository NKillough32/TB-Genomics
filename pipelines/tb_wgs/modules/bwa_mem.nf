process BWA_MEM {
  tag "${sample_id}"
  container "quay.io/biocontainers/bwa:0.7.18--he4a0461_1"

  input:
  tuple val(sample_id), path(reads)
  path reference

  output:
  tuple val(sample_id), path("${sample_id}.mapped.bam"), path("${sample_id}.mapped.bam.bai"), emit: mapped

  script:
  """
  # Production command placeholder:
  # Use one or two FASTQs from ${reads}; do not fabricate an empty R2 for single-end data.
  # bwa mem ${reference} <R1> [R2] | samtools sort -o ${sample_id}.mapped.bam
  # samtools index ${sample_id}.mapped.bam
  touch ${sample_id}.mapped.bam
  touch ${sample_id}.mapped.bam.bai
  """
}
