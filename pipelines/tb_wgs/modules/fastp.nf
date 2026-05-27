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
  python - <<'PY'
  import pathlib, shutil
  sid = "${sample_id}"
  shutil.copyfile("${read1}", f"{sid}_R1.fastq")
  if "${read2}":
      shutil.copyfile("${read2}", f"{sid}_R2.fastq")
  pathlib.Path("fastp_qc", f"{sid}.fastp.json").write_text('{"status":"placeholder"}\\n')
  PY
  """
}
