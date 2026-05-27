process FASTP {
  tag "fastp"
  container "quay.io/biocontainers/fastp:0.23.4--hadf994f_3"

  input:
  tuple val(sample_id), path(read1), path(read2)

  output:
  tuple val(sample_id), path("${sample_id}_R*.fastq"), emit: cleaned_reads
  path "fastp_qc", emit: qc

  script:
  """
  mkdir -p fastp_qc
  if [[ -n "${read2}" && -f "${read2}" ]]; then
    fastp \
      --in1 "${read1}" \
      --in2 "${read2}" \
      --out1 "${sample_id}_R1.fastq" \
      --out2 "${sample_id}_R2.fastq" \
      --json "fastp_qc/${sample_id}.fastp.json" \
      --html "fastp_qc/${sample_id}.fastp.html" \
      --thread ${task.cpus}
  else
    fastp \
      --in1 "${read1}" \
      --out1 "${sample_id}_R1.fastq" \
      --json "fastp_qc/${sample_id}.fastp.json" \
      --html "fastp_qc/${sample_id}.fastp.html" \
      --thread ${task.cpus}
  fi
  """
}
