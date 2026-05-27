process BWA_MEM {
  tag "bwa_mem"
  container "quay.io/biocontainers/bwa:0.7.18--he4a0461_1"

  input:
  path cleaned_reads
  path reference

  output:
  path "mapped.bam", emit: bam

  script:
  """
  # Production command placeholder:
  # bwa mem ${reference} <reads> | samtools sort -o mapped.bam
  touch mapped.bam
  """
}
