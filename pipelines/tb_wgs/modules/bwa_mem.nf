process BWA_INDEX {
  tag "reference"
  container "pegi3s/bwa_samtools:latest"

  input:
  path reference

  output:
  tuple path(reference), path("${reference}.*"), emit: indexed_reference

  script:
  """
  bwa index "${reference}"
  """
}

process BWA_MEM {
  tag "${sample_id}"
  container "pegi3s/bwa_samtools:latest"

  input:
  tuple val(sample_id), path(reads)
  tuple path(reference), path(index_files)

  output:
  tuple val(sample_id), path("${sample_id}.mapped.bam"), path("${sample_id}.mapped.bam.bai"), emit: mapped

  script:
  """
  bwa mem -t ${task.cpus} -R '@RG\\tID:${sample_id}\\tSM:${sample_id}\\tPL:ILLUMINA' "${reference}" ${reads} \
    | samtools sort -@ ${task.cpus} -o "${sample_id}.mapped.bam" -
  samtools index "${sample_id}.mapped.bam"
  """
}
