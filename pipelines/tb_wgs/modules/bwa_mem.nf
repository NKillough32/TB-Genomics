process BWA_MEM {
  tag "${sample_id}"
  container "quay.io/biocontainers/bwa:0.7.18--he4a0461_1"

  input:
  tuple val(sample_id), path(read1), path(read2)
  path reference

  output:
  tuple val(sample_id), path("${sample_id}.mapped.bam"), path("${sample_id}.mapped.bam.bai"), emit: mapped

  script:
  """
  # Production command placeholder:
  # bwa mem ${reference} ${read1} ${read2} | samtools sort -o ${sample_id}.mapped.bam
  # samtools index ${sample_id}.mapped.bam
  touch ${sample_id}.mapped.bam
  touch ${sample_id}.mapped.bam.bai
  """
}
