process FASTP {
  tag "fastp"
  container "quay.io/biocontainers/fastp:0.23.4--hadf994f_3"

  input:
  path sample_sheet

  output:
  path "fastp_cleaned_reads", emit: cleaned_reads
  path "fastp_qc", emit: qc

  script:
  """
  mkdir -p fastp_cleaned_reads fastp_qc
  python - <<'PY'
  import csv, pathlib, shutil
  with open("${sample_sheet}", newline="") as handle:
      for row in csv.DictReader(handle):
          sid = row["sample_id"]
          r1 = pathlib.Path(row["fastq_1"])
          r2 = pathlib.Path(row.get("fastq_2") or "") if row.get("fastq_2") else None
          shutil.copyfile(r1, pathlib.Path("fastp_cleaned_reads") / f"{sid}_R1.fastq")
          if r2:
              shutil.copyfile(r2, pathlib.Path("fastp_cleaned_reads") / f"{sid}_R2.fastq")
          pathlib.Path("fastp_qc", f"{sid}.fastp.json").write_text('{"status":"placeholder"}\\n')
  PY
  """
}
