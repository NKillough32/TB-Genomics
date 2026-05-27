process BCFTOOLS_CALL {
  tag "bcftools_call"
  container "quay.io/biocontainers/bcftools:1.21--h3a4d415_1"

  input:
  path bam
  path reference

  output:
  path "variants.vcf.gz", emit: vcf

  script:
  """
  # Production command placeholder:
  # bcftools mpileup -f ${reference} ${bam} | bcftools call -mv -Oz -o variants.vcf.gz
  python - <<'PY'
  import gzip
  with gzip.open("variants.vcf.gz", "wt") as handle:
      handle.write("##fileformat=VCFv4.2\\n#CHROM\\tPOS\\tID\\tREF\\tALT\\tQUAL\\tFILTER\\tINFO\\n")
  PY
  """
}
