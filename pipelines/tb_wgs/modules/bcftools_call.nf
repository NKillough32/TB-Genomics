process BCFTOOLS_CALL {
  tag "${sample_id}"
  container "quay.io/biocontainers/bcftools:1.21--h3a4d415_1"

  input:
  tuple val(sample_id), path(bam), path(bai)
  path reference

  output:
  tuple val(sample_id), path("${sample_id}.variants.vcf.gz"), emit: vcf

  script:
  """
  # Production command placeholder:
  # bcftools mpileup -f ${reference} ${bam} | bcftools call -mv -Oz -o ${sample_id}.variants.vcf.gz
  # Requires sorted BAM plus index: ${bam} and ${bai}.
  python - <<'PY'
  import gzip
  with gzip.open("${sample_id}.variants.vcf.gz", "wt") as handle:
      handle.write("##fileformat=VCFv4.2\\n#CHROM\\tPOS\\tID\\tREF\\tALT\\tQUAL\\tFILTER\\tINFO\\n")
  PY
  """
}
