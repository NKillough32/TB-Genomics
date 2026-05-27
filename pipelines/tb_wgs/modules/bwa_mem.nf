process BWA_MEM {
  tag "${sample_id}"
  container "pegi3s/bwa_samtools:latest"

  input:
  tuple val(sample_id), path(reads)
  path reference

  output:
  tuple val(sample_id), path("${sample_id}.mapped.bam"), path("${sample_id}.mapped.bam.bai"), emit: mapped

  script:
  """
  bwa index "${reference}"
  bwa mem -t ${task.cpus} -R '@RG\\tID:${sample_id}\\tSM:${sample_id}\\tPL:ILLUMINA' "${reference}" ${reads} \
    | samtools sort -@ ${task.cpus} -o "${sample_id}.mapped.bam" -
  samtools index "${sample_id}.mapped.bam"
  """
}
