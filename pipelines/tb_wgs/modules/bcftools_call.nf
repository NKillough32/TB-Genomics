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
  bcftools mpileup -Ou -f "${reference}" "${bam}" \
    | bcftools call -mv -Oz -o "${sample_id}.variants.vcf.gz"
  bcftools index -t "${sample_id}.variants.vcf.gz"
  """
}
